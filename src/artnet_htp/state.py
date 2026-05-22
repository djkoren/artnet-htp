"""Shared, thread-safe state for the merger.

Receiver (asyncio thread) writes via update_source / mark_*; the sender thread
reads via compute_merge / bump_sequence. All public methods acquire the lock.
Bytes copies are made under the lock and returned; callers do their I/O outside.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .protocol import DMX_MAX_CHANNELS


# An all-zeros DMX universe, shared as a sentinel.
_ZEROS = bytes(DMX_MAX_CHANNELS)


@dataclass(slots=True)
class SourceEntry:
    data: bytes = _ZEROS         # always normalized to 512 bytes
    last_seen: float = 0.0       # monotonic
    packet_count: int = 0


@dataclass(slots=True)
class UnknownSourceEntry:
    first_seen: float
    last_seen: float
    packet_count: int = 0


@dataclass(slots=True)
class OutputSpec:
    ip: str
    label: str = ""
    broadcast: bool = False
    port: int = 6454
    # Universes this output receives. The sender filters by membership so
    # different controllers can get different universe subsets. Empty list
    # means "no universes" — the output is configured but receives nothing.
    universes: tuple[int, ...] = ()


@dataclass(slots=True)
class SourceSpec:
    ip: str
    label: str = ""
    mode: str = "htp"     # "htp" or "priority"
    priority: int = 100   # higher = wins when mode == "priority"


def htp_merge(streams: list[bytes]) -> bytes:
    """Channel-wise max across N 512-byte streams. Returns 512 bytes."""
    if not streams:
        return _ZEROS
    if len(streams) == 1:
        return streams[0]
    # bytes(map(max, *streams)) is the tightest stdlib path here. Bytes objects
    # are sequences of ints, so max() on three or more is per-position max.
    return bytes(map(max, *streams))


def priority_merge(streams_with_priority: list[tuple[bytes, int]]) -> bytes:
    """Pick the highest-priority source whose current frame has any non-zero
    channel, and use its data exclusively. If no source has non-zero data,
    return zeros.

    `streams_with_priority` is a list of `(dmx_512_bytes, priority_int)` pairs;
    HIGHER priority numbers win.
    """
    if not streams_with_priority:
        return _ZEROS
    # any(b for b in bytes) short-circuits on first non-zero byte.
    for data, _ in sorted(streams_with_priority, key=lambda x: -x[1]):
        if any(data):
            return data
    return _ZEROS


def tiered_merge(
    streams_by_mode: list[tuple[bytes, str, int]],
) -> bytes:
    """Two-tier merge: priority-mode sources override htp-mode sources.

    Input: list of (data, mode, priority) where mode is "htp" or "priority".

    Algorithm:
      1. Among priority-mode sources, find any with non-zero data. If found,
         the one with the highest `priority` number wins exclusively.
      2. Otherwise, HTP-merge all htp-mode sources.
      3. If neither group has anything to contribute, return zeros.
    """
    priority_streams = [(d, p) for d, mode, p in streams_by_mode if mode == "priority"]
    if priority_streams:
        # Sort by priority DESC so the first active one is the winner.
        for data, _ in sorted(priority_streams, key=lambda x: -x[1]):
            if any(data):
                return data
        # All priority sources are blacked out → fall through to HTP tier.

    htp_streams = [d for d, mode, _ in streams_by_mode if mode == "htp"]
    if htp_streams:
        return htp_merge(htp_streams)
    return _ZEROS


class MergerState:
    """Thread-safe state container. Single lock; no nested locking."""

    def __init__(
        self,
        *,
        source_timeout_s: float = 2.5,
        send_keepalive_when_silent: bool = True,
        auto_allow_unknown_sources: bool = False,
    ) -> None:
        self._lock = threading.Lock()

        # Configuration
        self._source_timeout_s = source_timeout_s
        self._send_keepalive_when_silent = send_keepalive_when_silent
        self._auto_allow_unknown_sources = auto_allow_unknown_sources
        self._sources_by_ip: dict[str, SourceSpec] = {}
        self._outputs: list[OutputSpec] = []
        self._universes: set[int] = set()

        # Per-source state, keyed by (source_ip, port_address)
        self._entries: dict[tuple[str, int], SourceEntry] = {}

        # Unknown sources observed but not in allowlist
        self._unknown: dict[str, UnknownSourceEntry] = {}

        # Per-output, per-universe outbound sequence counters (1..255)
        self._seq: dict[tuple[str, int], int] = {}

        # Send-side counters per output IP
        self._send_packet_count: dict[str, int] = {}

        # Other counters
        self._malformed = 0
        self._artpoll_count = 0

    # ----- config hot-apply -----
    def apply_config(
        self,
        *,
        sources: list[SourceSpec],
        outputs: list[OutputSpec],
        universes: list[int],
        source_timeout_s: float,
        send_keepalive_when_silent: bool,
        auto_allow_unknown_sources: bool,
    ) -> None:
        with self._lock:
            self._sources_by_ip = {s.ip: s for s in sources}
            self._outputs = list(outputs)
            self._universes = set(universes)
            self._source_timeout_s = source_timeout_s
            self._send_keepalive_when_silent = send_keepalive_when_silent
            self._auto_allow_unknown_sources = auto_allow_unknown_sources

            # Drop entries for sources no longer allowlisted
            allowed = set(self._sources_by_ip)
            self._entries = {
                k: v for k, v in self._entries.items() if k[0] in allowed
            }
            # Drop unknown entries that have now been allowlisted
            for ip in list(self._unknown):
                if ip in allowed:
                    del self._unknown[ip]
            # Drop send counters for outputs/universes no longer configured
            output_ips = {o.ip for o in self._outputs}
            self._seq = {
                k: v for k, v in self._seq.items()
                if k[0] in output_ips and k[1] in self._universes
            }
            self._send_packet_count = {
                ip: c for ip, c in self._send_packet_count.items()
                if ip in output_ips
            }

    # ----- receive side -----
    def update_source(
        self, source_ip: str, port_address: int, data: bytes, now: float | None = None
    ) -> bool:
        """Record an incoming DMX frame.

        Returns True if accepted (source in allowlist or auto-allow on), False otherwise.
        Caller (receiver) should still call mark_unknown_source(source_ip) on rejection
        so the UI surfaces it.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            if source_ip not in self._sources_by_ip:
                if not self._auto_allow_unknown_sources:
                    return False
                # auto-allow: add to allowlist on the fly
                self._sources_by_ip[source_ip] = SourceSpec(ip=source_ip, label="(auto-allowed)")
                self._unknown.pop(source_ip, None)

            if port_address not in self._universes:
                return False  # not a universe we care about

            # Normalize to 512 bytes
            n = len(data)
            if n == DMX_MAX_CHANNELS:
                norm = bytes(data)
            elif n < DMX_MAX_CHANNELS:
                norm = bytes(data) + _ZEROS[: DMX_MAX_CHANNELS - n]
            else:
                norm = bytes(data[:DMX_MAX_CHANNELS])

            key = (source_ip, port_address)
            entry = self._entries.get(key)
            if entry is None:
                entry = SourceEntry()
                self._entries[key] = entry
            entry.data = norm
            entry.last_seen = now
            entry.packet_count += 1
            return True

    def mark_unknown_source(self, source_ip: str, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        with self._lock:
            if source_ip in self._sources_by_ip:
                return
            u = self._unknown.get(source_ip)
            if u is None:
                self._unknown[source_ip] = UnknownSourceEntry(
                    first_seen=now, last_seen=now, packet_count=1
                )
            else:
                u.last_seen = now
                u.packet_count += 1

    def mark_malformed(self) -> None:
        with self._lock:
            self._malformed += 1

    def mark_artpoll(self) -> None:
        with self._lock:
            self._artpoll_count += 1

    def allow_source(self, ip: str, label: str = "") -> None:
        with self._lock:
            if ip not in self._sources_by_ip:
                self._sources_by_ip[ip] = SourceSpec(ip=ip, label=label or ip)
            self._unknown.pop(ip, None)

    def block_source(self, ip: str) -> None:
        with self._lock:
            self._sources_by_ip.pop(ip, None)
            # remove its data so the next merge drops its contribution
            for key in list(self._entries):
                if key[0] == ip:
                    del self._entries[key]

    # ----- send side -----
    def compute_merge(
        self, port_address: int, now: float | None = None
    ) -> bytes | None:
        """Return merged 512-byte DMX for one universe.

        Returns None when no source has been seen recently AND keepalive is off.
        Always returns 512 bytes when keepalive is on (zeros if silent).
        Uses the two-tier merge: priority-mode sources override htp-mode sources.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            if port_address not in self._universes:
                return None
            timeout = self._source_timeout_s
            # Build per-source (mode, priority) lookup
            meta = {ip: (s.mode, s.priority) for ip, s in self._sources_by_ip.items()}
            streams: list[tuple[bytes, str, int]] = []
            for (ip, pa), entry in self._entries.items():
                if pa != port_address:
                    continue
                if now - entry.last_seen > timeout:
                    continue
                mode, pri = meta.get(ip, ("htp", 100))
                streams.append((entry.data, mode, pri))
            keepalive = self._send_keepalive_when_silent

        # Compute outside the lock (pure CPU)
        if not streams:
            return _ZEROS if keepalive else None
        return tiered_merge(streams)

    def bump_sequence(self, output_ip: str, port_address: int) -> int:
        """Return the next outbound sequence number for this (output, universe).

        Wraps 1..255. 0 is reserved by the spec to mean "sequencing disabled" so we
        never emit it.
        """
        with self._lock:
            key = (output_ip, port_address)
            current = self._seq.get(key, 0)
            nxt = (current % 255) + 1
            self._seq[key] = nxt
            return nxt

    def record_send(self, output_ip: str, count: int = 1) -> None:
        with self._lock:
            self._send_packet_count[output_ip] = (
                self._send_packet_count.get(output_ip, 0) + count
            )

    # ----- snapshot for UI / monitoring -----
    def snapshot(self, now: float | None = None) -> dict:
        if now is None:
            now = time.monotonic()
        with self._lock:
            timeout = self._source_timeout_s

            # Aggregate per-source (across all its universes)
            per_source: dict[str, dict] = {}
            for ip, spec in self._sources_by_ip.items():
                per_source[ip] = {
                    "ip": ip,
                    "label": spec.label,
                    "mode": spec.mode,
                    "priority": spec.priority,
                    "allowed": True,
                    "alive": False,
                    "active": False,
                    "universes": [],
                    "packet_count": 0,
                    "last_seen_age_s": None,
                }
            for (ip, pa), entry in self._entries.items():
                spec = self._sources_by_ip.get(ip)
                bucket = per_source.setdefault(
                    ip,
                    {
                        "ip": ip,
                        "label": spec.label if spec else ip,
                        "mode": spec.mode if spec else "htp",
                        "priority": spec.priority if spec else 100,
                        "allowed": ip in self._sources_by_ip,
                        "alive": False,
                        "active": False,
                        "universes": [],
                        "packet_count": 0,
                        "last_seen_age_s": None,
                    },
                )
                age = now - entry.last_seen
                alive = age <= timeout
                # "Active" = alive AND last frame has any non-zero channel.
                # Used by the priority merge to decide which source wins;
                # surfaced to the UI so operators can see "Madrix is alive
                # but blacked out, FPP is active".
                active = alive and any(entry.data)
                bucket["universes"].append({
                    "port_address": pa,
                    "alive": alive,
                    "active": active,
                    "age_s": age,
                    "packet_count": entry.packet_count,
                })
                bucket["packet_count"] += entry.packet_count
                if active:
                    bucket["active"] = True
                if bucket["last_seen_age_s"] is None or age < bucket["last_seen_age_s"]:
                    bucket["last_seen_age_s"] = age
                    bucket["alive"] = alive

            return {
                "sources": list(per_source.values()),
                "outputs": [
                    {
                        "ip": o.ip,
                        "label": o.label,
                        "broadcast": o.broadcast,
                        "port": o.port,
                        "packet_count": self._send_packet_count.get(o.ip, 0),
                    }
                    for o in self._outputs
                ],
                "universes": sorted(self._universes),
                "unknown_sources": [
                    {
                        "ip": ip,
                        "first_seen_age_s": now - u.first_seen,
                        "last_seen_age_s": now - u.last_seen,
                        "packet_count": u.packet_count,
                    }
                    for ip, u in self._unknown.items()
                ],
                "malformed_count": self._malformed,
                "artpoll_count": self._artpoll_count,
                "settings": {
                    "source_timeout_s": self._source_timeout_s,
                    "send_keepalive_when_silent": self._send_keepalive_when_silent,
                    "auto_allow_unknown_sources": self._auto_allow_unknown_sources,
                },
            }

    # ----- accessors for sender / receiver -----
    def outputs_snapshot(self) -> list[OutputSpec]:
        """Stable copy for the sender loop. Sender iterates outside the lock."""
        with self._lock:
            return list(self._outputs)

    def universes_snapshot(self) -> list[int]:
        with self._lock:
            return sorted(self._universes)

    def is_allowed(self, ip: str) -> bool:
        with self._lock:
            return ip in self._sources_by_ip or self._auto_allow_unknown_sources

    def get_merged_for_preview(self, port_address: int) -> bytes | None:
        """Like compute_merge but never returns zeros for keepalive — returns
        None when no source is alive, so the UI can show 'silent'. Honors each
        source's mode/priority.
        """
        with self._lock:
            if port_address not in self._universes:
                return None
            now = time.monotonic()
            timeout = self._source_timeout_s
            meta = {ip: (s.mode, s.priority) for ip, s in self._sources_by_ip.items()}
            streams: list[tuple[bytes, str, int]] = []
            for (ip, pa), entry in self._entries.items():
                if pa != port_address:
                    continue
                if now - entry.last_seen > timeout:
                    continue
                mode, pri = meta.get(ip, ("htp", 100))
                streams.append((entry.data, mode, pri))
        if not streams:
            return None
        return tiered_merge(streams)
