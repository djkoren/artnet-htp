"""ArtPoll → ArtPollReply service.

Responsibilities:
- Reply to inbound ArtPoll with one or more ArtPollReply packets covering all
  configured universes (grouped into "binds" of up to 4 universes sharing
  Net/Sub).
- Broadcast unsolicited ArtPollReply every 2.5s as required by the Art-Net spec.

Universes are organized by Net (high 7 bits) and Sub (next 4 bits); a single
ArtPollReply describes one (Net, Sub) group with up to 4 ports.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from dataclasses import dataclass

from .protocol import (
    ARTNET_PORT,
    PortInfo,
    build_artpoll_reply,
)
from .state import MergerState

log = logging.getLogger(__name__)

POLL_REPLY_INTERVAL_S = 2.5


@dataclass(slots=True)
class NodeIdentity:
    bind_ip: str           # advertised IP — must be a real interface IP, not 0.0.0.0
    mac: bytes             # 6 bytes
    short_name: str
    long_name: str
    node_report: str = "OK - HTP merger ready"


class PollReplyService:
    """Sends ArtPollReply packets via a single UDP socket.

    All sends are non-blocking and fast; this lives in the asyncio loop.
    """

    def __init__(self, state: MergerState, identity: NodeIdentity) -> None:
        self.state = state
        self.identity = identity
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ----- public API -----
    async def respond_to(self, src_ip: str, src_port: int) -> None:
        """Reply to an ArtPoll directly. Sent unicast to the polling node."""
        for pkt in self._build_replies():
            self._sendto(pkt, (src_ip, src_port if src_port else ARTNET_PORT))

    def broadcast_now(self) -> None:
        """Send an unsolicited ArtPollReply via the limited broadcast address."""
        for pkt in self._build_replies():
            self._sendto(pkt, ("255.255.255.255", ARTNET_PORT))

    async def run_periodic(self) -> None:
        """Coroutine that broadcasts an ArtPollReply every POLL_REPLY_INTERVAL_S."""
        try:
            while not self._stop.is_set():
                self.broadcast_now()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_REPLY_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # ----- internals -----
    def _sendto(self, pkt: bytes, addr: tuple[str, int]) -> None:
        try:
            self._sock.sendto(pkt, addr)
        except OSError as e:
            log.warning("ArtPollReply send to %s failed: %s", addr, e)

    def _build_replies(self) -> list[bytes]:
        universes = self.state.universes_snapshot()
        groups = _group_universes(universes)
        if not groups:
            # No universes configured — still emit one "empty" reply so the node
            # is discoverable. Use Net=0, Sub=0.
            groups = [(0, 0, [])]

        ip_bytes = _ipv4_to_bytes(self.identity.bind_ip)
        replies: list[bytes] = []
        for bind_index, (net, sub, uni_list) in enumerate(groups, start=1):
            ports = [
                PortInfo(universe_4bit=u, is_input=True, is_output=True)
                for u in uni_list
            ]
            reply = build_artpoll_reply(
                bind_ip=ip_bytes,
                mac=self.identity.mac,
                short_name=self.identity.short_name,
                long_name=self.identity.long_name,
                node_report=self.identity.node_report,
                net_switch=net,
                sub_switch=sub,
                ports=ports,
                bind_index=bind_index,
            )
            replies.append(reply)
        return replies


# --------------------------- helpers --------------------------------------- #

def _group_universes(universes: list[int]) -> list[tuple[int, int, list[int]]]:
    """Group port-addresses into ArtPollReply binds: max 4 ports per (Net, Sub)."""
    by_netsub: dict[tuple[int, int], list[int]] = {}
    for pa in universes:
        net = (pa >> 8) & 0x7F
        sub = (pa >> 4) & 0x0F
        uni = pa & 0x0F
        by_netsub.setdefault((net, sub), []).append(uni)

    out: list[tuple[int, int, list[int]]] = []
    for (net, sub), unis in sorted(by_netsub.items()):
        unis.sort()
        for i in range(0, len(unis), 4):
            out.append((net, sub, unis[i:i + 4]))
    return out


def _ipv4_to_bytes(ip: str) -> bytes:
    """Convert dotted-quad to 4 bytes. Raises on invalid input."""
    parts = ip.split(".")
    if len(parts) != 4:
        raise ValueError(f"invalid IPv4: {ip!r}")
    out = bytearray(4)
    for i, p in enumerate(parts):
        v = int(p)
        if not 0 <= v <= 255:
            raise ValueError(f"invalid IPv4 octet in {ip!r}")
        out[i] = v
    return bytes(out)


def detect_local_ip(probe: str = "1.1.1.1") -> str:
    """Best-effort: pick the local IP the kernel would use to reach the probe.

    Useful when bind_ip is 0.0.0.0 — we still need a real IP to advertise in
    ArtPollReply so consoles can route back to us.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((probe, 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def detect_mac() -> bytes:
    """Return 6-byte MAC of any interface. Falls back to a stable random MAC."""
    node = uuid.getnode()
    # uuid.getnode() returns a 48-bit int; bit 41 set means it's randomly generated
    return node.to_bytes(6, "big")
