"""Asyncio UDP receiver for ArtNet.

Routes ArtDmx to MergerState and ArtPoll to the supplied callback. Malformed and
unauthorized packets are counted on the state; nothing is raised to the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .protocol import (
    ARTNET_PORT,
    OP_DMX,
    OP_POLL,
    parse_artdmx,
    parse_artpoll,
    peek_opcode,
)
from .state import MergerState

log = logging.getLogger(__name__)

OnArtPoll = Callable[[str, int], Awaitable[None] | None]


class ArtNetReceiver(asyncio.DatagramProtocol):
    def __init__(self, state: MergerState, on_artpoll: OnArtPoll | None = None) -> None:
        self.state = state
        self.on_artpoll = on_artpoll
        self.transport: asyncio.DatagramTransport | None = None
        self._loop = asyncio.get_event_loop()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock is not None:
            log.info("artnet receiver listening on %s", sock.getsockname())

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        src_ip, src_port = addr
        opcode = peek_opcode(data)
        if opcode is None:
            return  # not ArtNet at all; silently drop

        if opcode == OP_DMX:
            pkt = parse_artdmx(data)
            if pkt is None:
                self.state.mark_malformed()
                return
            accepted = self.state.update_source(src_ip, pkt.port_address, pkt.data)
            if not accepted:
                # Either source not allowlisted, or universe not configured.
                # We can't tell which here without re-querying state; log the
                # source as "unknown" only if it's not in the allowlist.
                if not self.state.is_allowed(src_ip):
                    self.state.mark_unknown_source(src_ip)
            return

        if opcode == OP_POLL:
            poll = parse_artpoll(data)
            if poll is None:
                self.state.mark_malformed()
                return
            self.state.mark_artpoll()
            if self.on_artpoll is not None:
                result = self.on_artpoll(src_ip, src_port)
                if asyncio.iscoroutine(result):
                    self._loop.create_task(result)
            return

        # Other opcodes (ArtPollReply from other nodes, ArtSync, etc.) ignored.

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        log.warning("receiver socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:  # type: ignore[override]
        if exc:
            log.warning("receiver connection lost: %s", exc)


async def start_receiver(
    state: MergerState,
    on_artpoll: OnArtPoll | None = None,
    *,
    bind_ip: str = "0.0.0.0",
    port: int = ARTNET_PORT,
) -> tuple[asyncio.DatagramTransport, ArtNetReceiver]:
    """Bind the receiver. Returns (transport, protocol). Close the transport to stop.

    `bind_ip` is what `socket.bind()` gets. In practice the controller passes
    "0.0.0.0" so we listen on every interface — accepting ArtNet from any
    network the Pi is on is almost always what the operator wants.

    If a non-default bind_ip is requested and turns out to be unavailable
    (EADDRNOTAVAIL — IP isn't on any live interface), we fall back to
    "0.0.0.0" rather than crashing. Previously this would crash-loop the
    service under systemd if the operator set a bind IP that DHCP later
    reassigned.
    """
    loop = asyncio.get_running_loop()

    async def _bind(addr: str) -> tuple[asyncio.DatagramTransport, ArtNetReceiver]:
        t, p = await loop.create_datagram_endpoint(
            lambda: ArtNetReceiver(state, on_artpoll),
            local_addr=(addr, port),
            allow_broadcast=True,
        )
        return t, p  # type: ignore[return-value]

    try:
        return await _bind(bind_ip)
    except OSError as e:
        # errno 99 = EADDRNOTAVAIL. Don't take the merger down — degrade to
        # 0.0.0.0 and warn loudly. Any other OSError is genuinely wrong
        # (port already in use, etc.) and should propagate.
        if e.errno != 99 or bind_ip == "0.0.0.0":
            raise
        log.warning(
            "couldn't bind UDP receiver to %s:%d (%s) — falling back to 0.0.0.0",
            bind_ip, port, e,
        )
        return await _bind("0.0.0.0")
