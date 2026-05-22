"""Fixed-rate ArtDmx sender.

Runs on a dedicated daemon thread (NOT asyncio) for tight timing — asyncio's
scheduler can drift several milliseconds at 25ms intervals under load, and DMX
is timing-sensitive.

Lock discipline: pull bytes out of MergerState under its lock, release, then
sendto() — never hold a lock across a syscall.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from .protocol import ARTNET_PORT, build_artdmx
from .state import MergerState

log = logging.getLogger(__name__)


class SenderThread(threading.Thread):
    def __init__(
        self,
        state: MergerState,
        *,
        send_rate_hz: float = 44.0,
        name: str = "artnet-sender",
    ) -> None:
        super().__init__(daemon=True, name=name)
        self.state = state
        self._stop = threading.Event()
        self._rate_lock = threading.Lock()
        self._send_rate_hz = max(1.0, min(60.0, send_rate_hz))

        self._unicast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    # ----- control -----
    def set_rate(self, hz: float) -> None:
        with self._rate_lock:
            self._send_rate_hz = max(1.0, min(60.0, hz))

    def stop(self, timeout: float | None = 1.0) -> None:
        self._stop.set()
        self.join(timeout)
        try:
            self._unicast_sock.close()
        except OSError:
            pass
        try:
            self._broadcast_sock.close()
        except OSError:
            pass

    # ----- main loop -----
    def run(self) -> None:
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            with self._rate_lock:
                period = 1.0 / self._send_rate_hz

            try:
                self._tick()
            except Exception:
                log.exception("sender tick crashed; continuing")

            next_tick += period
            sleep = next_tick - time.perf_counter()
            if sleep > 0:
                # wait() returns True if the event is set — break out cleanly
                if self._stop.wait(timeout=sleep):
                    break
            else:
                # We're behind schedule (e.g. system suspend, lock contention).
                # Reset the clock rather than sending bursts to catch up.
                if sleep < -0.5:
                    log.warning("sender behind by %.3fs; resyncing", -sleep)
                next_tick = time.perf_counter()

    # ----- one tick -----
    def _tick(self) -> None:
        outputs = self.state.outputs_snapshot()
        if not outputs:
            return
        universes = self.state.universes_snapshot()
        if not universes:
            return

        for u in universes:
            merged = self.state.compute_merge(port_address=u)
            if merged is None:
                continue  # silent and keepalive off

            for out in outputs:
                # v0.3.0: each output declares which universes it receives.
                # If its list is non-empty and `u` isn't in it, skip — this
                # lets one merger fan different universe subsets to different
                # controllers (Roof gets 1-4, Truss gets 5-8, etc.). Empty
                # list means "no universes" — output stays configured but
                # gets nothing until populated.
                if out.universes and u not in out.universes:
                    continue
                if not out.universes:
                    continue
                seq = self.state.bump_sequence(out.ip, u)
                pkt = build_artdmx(port_address=u, data=merged, sequence=seq)
                sock = self._broadcast_sock if out.broadcast else self._unicast_sock
                try:
                    sock.sendto(pkt, (out.ip, out.port))
                except OSError as e:
                    log.warning("send to %s:%d failed: %s", out.ip, out.port, e)
                    continue
                self.state.record_send(out.ip)
