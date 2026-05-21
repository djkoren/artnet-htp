"""Synthetic ArtNet source / listener for testing the merger.

Examples:
    # Send a flat level of 100 on universe 0 at 30Hz to localhost
    python tools/synth.py --universe 0 --level 100 --rate 30 --dest 127.0.0.1

    # Send a sine pattern on universe 1
    python tools/synth.py --universe 1 --pattern sine --rate 30

    # Listen and print incoming DMX packets (first 16 channels) and discoveries
    python tools/synth.py --listen --channels 16
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path

# Make `src` importable without `pip install -e .` for ad-hoc runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from artnet_htp.protocol import (
    ARTNET_MAGIC,
    ARTNET_PORT,
    OP_DMX,
    OP_POLL_REPLY,
    build_artdmx,
    parse_artdmx,
    peek_opcode,
)


def emit(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if args.source_ip:
        # Bind to a specific local interface IP so the receiver sees this as
        # the source. On macOS, 127.0.0.X aliases need to be brought up first
        # with: sudo ifconfig lo0 alias 127.0.0.X up
        sock.bind((args.source_ip, 0))

    period = 1.0 / args.rate
    seq = 0
    t0 = time.perf_counter()
    next_tick = t0

    print(
        f"emitting universe={args.universe} pattern={args.pattern} "
        f"level={args.level} rate={args.rate}Hz -> {args.dest}:{args.port}",
        file=sys.stderr,
    )

    try:
        while True:
            now = time.perf_counter()
            elapsed = now - t0
            data = _generate_dmx(args.pattern, args.level, args.channels, elapsed)
            seq = (seq % 255) + 1  # 1..255, never 0 (0 = sequencing disabled)
            pkt = build_artdmx(
                port_address=args.universe,
                data=data,
                sequence=seq,
            )
            sock.sendto(pkt, (args.dest, args.port))

            next_tick += period
            sleep = next_tick - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # drifted; reset baseline
                next_tick = time.perf_counter()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


def _generate_dmx(pattern: str, level: int, channels: int, elapsed: float) -> bytes:
    if channels < 2 or channels > 512 or (channels & 1):
        raise ValueError("channels must be 2..512 and even")

    if pattern == "flat":
        return bytes([level]) * channels

    if pattern == "ramp":
        # cycle channel 1 from 0..255 over 5 seconds, rest flat at level
        ch1 = int((elapsed % 5.0) / 5.0 * 256) & 0xFF
        out = bytearray([level] * channels)
        out[0] = ch1
        return bytes(out)

    if pattern == "sine":
        # all channels modulated by a 0.5Hz sine centered on level, ±64
        phase = elapsed * 2 * math.pi * 0.5
        v = int(max(0, min(255, level + 64 * math.sin(phase))))
        return bytes([v]) * channels

    if pattern == "chase":
        # one channel at 255 cycling through the universe at 1Hz
        idx = int(elapsed) % channels
        out = bytearray(channels)
        out[idx] = 255
        return bytes(out)

    raise ValueError(f"unknown pattern: {pattern!r}")


def listen(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind((args.bind, args.port))

    print(
        f"listening on {args.bind}:{args.port} "
        f"(filter universe={args.universe if args.universe is not None else 'any'})",
        file=sys.stderr,
    )

    last_seq: dict[tuple[str, int], int] = {}
    last_print: dict[tuple[str, int], float] = {}

    while True:
        try:
            data, (src_ip, _src_port) = sock.recvfrom(2048)
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
            return

        op = peek_opcode(data)
        if op is None:
            continue

        if op == OP_DMX:
            pkt = parse_artdmx(data)
            if pkt is None:
                print(f"  [malformed ArtDmx from {src_ip}]", file=sys.stderr)
                continue
            if args.universe is not None and pkt.port_address != args.universe:
                continue
            key = (src_ip, pkt.port_address)
            prev = last_seq.get(key)
            drop_marker = ""
            if prev is not None and pkt.sequence != 0:
                expected = (prev % 255) + 1
                if pkt.sequence != expected and pkt.sequence != prev:
                    drop_marker = f"  [seq jump {prev}->{pkt.sequence}]"
            last_seq[key] = pkt.sequence

            # Throttle prints to ~ args.print_rate per (src, universe)
            now = time.perf_counter()
            if now - last_print.get(key, 0.0) < 1.0 / args.print_rate:
                continue
            last_print[key] = now

            preview = " ".join(f"{b:3d}" for b in pkt.data[: args.channels])
            print(
                f"DMX  src={src_ip:<15} u={pkt.port_address:<5} seq={pkt.sequence:<3}"
                f" len={len(pkt.data):<3}  [{preview}]{drop_marker}",
                flush=True,
            )
            continue

        if op == OP_POLL_REPLY:
            # decode short name + IP for visibility
            if len(data) < 44:
                continue
            ip = ".".join(str(b) for b in data[10:14])
            short_name = bytes(data[26:44]).split(b"\x00", 1)[0].decode("ascii", "replace")
            print(f"REPLY src={src_ip:<15} adv_ip={ip:<15} name={short_name!r}", flush=True)
            continue

        # other opcodes — just note them
        print(f"PKT  src={src_ip:<15} op=0x{op:04X} len={len(data)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Synthetic ArtNet emitter / listener.")
    p.add_argument("--listen", action="store_true",
                   help="Listen and print incoming ArtNet instead of emitting.")
    p.add_argument("--dest", default="127.0.0.1",
                   help="Destination IP (emit mode). Default 127.0.0.1.")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Bind IP (listen mode). Default 0.0.0.0.")
    p.add_argument("--port", type=int, default=ARTNET_PORT,
                   help="UDP port. Default 6454.")
    p.add_argument("--universe", type=int_or_none, default=None,
                   help="15-bit port-address. Required in emit mode; "
                        "in listen mode filters to this universe (omit for all).")
    p.add_argument("--rate", type=float, default=30.0,
                   help="Send rate Hz (emit mode). Default 30.")
    p.add_argument("--level", type=int, default=128,
                   help="DMX level 0..255 (emit mode). Default 128.")
    p.add_argument("--channels", type=int, default=512,
                   help="Channel count to emit / preview width in listen mode. Default 512 emit / "
                        "see --channels in listen mode for preview width.")
    p.add_argument("--pattern", choices=["flat", "ramp", "sine", "chase"],
                   default="flat",
                   help="Emit pattern. Default flat.")
    p.add_argument("--broadcast", action="store_true",
                   help="Set SO_BROADCAST on the emit socket.")
    p.add_argument("--source-ip", default=None,
                   help="Bind to this local IP for emitting. Lets the receiver "
                        "see a specific source IP (useful with loopback aliases "
                        "for multi-source HTP testing on one machine).")
    p.add_argument("--print-rate", type=float, default=2.0,
                   help="Max prints per second per (source,universe) in listen mode. Default 2.")
    args = p.parse_args(argv)

    if args.listen:
        # In listen mode, default --channels preview to 16 for readability.
        if args.channels == 512:
            args.channels = 16
        try:
            listen(args)
        except KeyboardInterrupt:
            pass
        return 0

    if args.universe is None:
        p.error("--universe is required in emit mode")
    if not 0 <= args.level <= 255:
        p.error("--level must be 0..255")
    emit(args)
    return 0


def int_or_none(s: str) -> int | None:
    if s == "" or s.lower() == "none":
        return None
    return int(s)


if __name__ == "__main__":
    raise SystemExit(main())
