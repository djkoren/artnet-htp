"""ArtNet packet pack/unpack.

References:
- Art-Net 4 specification, Artistic Licence Holdings Ltd.
- https://art-net.org.uk/downloads/art-net.pdf
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

ARTNET_MAGIC = b"Art-Net\x00"
ARTNET_PORT = 6454

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000
OP_SYNC = 0x5200

PROT_VER_HI = 0
PROT_VER_LO = 14  # minimum acceptable

DMX_MAX_CHANNELS = 512
PORT_ADDRESS_MAX = 0x7FFF  # 15-bit


@dataclass(frozen=True, slots=True)
class ArtDmxPacket:
    sequence: int
    physical: int
    port_address: int  # 15-bit
    data: bytes        # 2..512 bytes, even length


@dataclass(frozen=True, slots=True)
class ArtPollPacket:
    talk_to_me: int
    priority: int


@dataclass(slots=True)
class PortInfo:
    """One port within an ArtPollReply 'bind' (group of up to 4 ports sharing Net/Sub)."""
    universe_4bit: int    # low 4 bits of port-address
    is_input: bool        # node receives this universe
    is_output: bool       # node sends this universe


def peek_opcode(packet: bytes) -> int | None:
    """Return the opcode if the magic header matches, else None."""
    if len(packet) < 10 or packet[:8] != ARTNET_MAGIC:
        return None
    return struct.unpack_from("<H", packet, 8)[0]


def parse_artdmx(packet: bytes) -> ArtDmxPacket | None:
    """Parse an ArtDmx packet. Returns None for any malformed input."""
    if len(packet) < 18 or packet[:8] != ARTNET_MAGIC:
        return None
    opcode, prot_hi, prot_lo, sequence, physical = struct.unpack_from(
        "<HBBBB", packet, 8
    )
    if opcode != OP_DMX:
        return None
    if prot_hi != PROT_VER_HI or prot_lo < PROT_VER_LO:
        return None

    sub_uni = packet[14]
    net = packet[15]
    port_address = (net << 8) | sub_uni
    if port_address > PORT_ADDRESS_MAX:
        return None

    length = struct.unpack_from(">H", packet, 16)[0]
    if length < 2 or length > DMX_MAX_CHANNELS or (length & 1):
        return None
    if len(packet) < 18 + length:
        return None

    data = bytes(packet[18:18 + length])
    return ArtDmxPacket(
        sequence=sequence,
        physical=physical,
        port_address=port_address,
        data=data,
    )


def build_artdmx(
    port_address: int,
    data: bytes,
    sequence: int = 0,
    physical: int = 0,
) -> bytes:
    """Build an ArtDmx packet.

    Args:
        port_address: 15-bit Art-Net port-address (Net<<8 | SubUni).
        data: DMX channel data. Must be 2..512 bytes and even length.
        sequence: 0 disables sequencing; 1..255 are valid sequence numbers.
        physical: informational physical port number on the sender (0-3).
    """
    if not 0 <= port_address <= PORT_ADDRESS_MAX:
        raise ValueError(f"port_address out of range: {port_address}")
    n = len(data)
    if n < 2 or n > DMX_MAX_CHANNELS or (n & 1):
        raise ValueError(f"data length must be 2..512 and even, got {n}")
    if not 0 <= sequence <= 255:
        raise ValueError(f"sequence must be 0..255, got {sequence}")
    if not 0 <= physical <= 255:
        raise ValueError(f"physical must be 0..255, got {physical}")

    sub_uni = port_address & 0xFF
    net = (port_address >> 8) & 0x7F

    header = struct.pack(
        "<8sHBBBBBB",
        ARTNET_MAGIC,
        OP_DMX,
        PROT_VER_HI,
        PROT_VER_LO,
        sequence,
        physical,
        sub_uni,
        net,
    )
    length = struct.pack(">H", n)
    return header + length + data


def parse_artpoll(packet: bytes) -> ArtPollPacket | None:
    """Parse an ArtPoll packet."""
    if len(packet) < 14 or packet[:8] != ARTNET_MAGIC:
        return None
    opcode, prot_hi, prot_lo, talk_to_me, priority = struct.unpack_from(
        "<HBBBB", packet, 8
    )
    if opcode != OP_POLL:
        return None
    if prot_hi != PROT_VER_HI or prot_lo < PROT_VER_LO:
        return None
    return ArtPollPacket(talk_to_me=talk_to_me, priority=priority)


# ArtPollReply field constants
_POLL_REPLY_LEN = 239  # bytes; minimum widely-compatible size

# PortType bits: bit 7 = output enabled, bit 6 = input enabled,
# lower 6 bits = protocol (0 = DMX512)
PORT_TYPE_DMX512 = 0x00
PORT_TYPE_OUTPUT = 0x80
PORT_TYPE_INPUT = 0x40


def build_artpoll_reply(
    *,
    bind_ip: bytes,
    mac: bytes,
    short_name: str,
    long_name: str,
    node_report: str,
    net_switch: int,      # 7 bits (high bits of port-address, >> 8)
    sub_switch: int,      # 4 bits (bits 7-4 of port-address)
    ports: list[PortInfo],
    bind_index: int = 1,
    firmware_version: int = 0x0100,
    oem: int = 0x00FF,
    esta_man: int = 0x0000,
    status1: int = 0xD0,  # indicators normal, port-address from front-panel
    status2: int = 0x0F,  # web-config + DHCP capable + 15-bit port-addrs supported
    status3: int = 0x00,
) -> bytes:
    """Build a single ArtPollReply for one 'bind' (up to 4 ports sharing Net/Sub).

    A node with more than 4 ports, or with ports under different Net/Sub,
    emits multiple ArtPollReply packets with incrementing bind_index.
    """
    if len(bind_ip) != 4:
        raise ValueError("bind_ip must be 4 bytes")
    if len(mac) != 6:
        raise ValueError("mac must be 6 bytes")
    if len(ports) > 4:
        raise ValueError(f"max 4 ports per ArtPollReply, got {len(ports)}")
    if not 0 <= net_switch <= 0x7F:
        raise ValueError(f"net_switch must be 0..127, got {net_switch}")
    if not 0 <= sub_switch <= 0x0F:
        raise ValueError(f"sub_switch must be 0..15, got {sub_switch}")

    buf = bytearray(_POLL_REPLY_LEN)
    buf[0:8] = ARTNET_MAGIC
    struct.pack_into("<H", buf, 8, OP_POLL_REPLY)
    buf[10:14] = bind_ip
    struct.pack_into("<H", buf, 14, ARTNET_PORT)
    struct.pack_into(">H", buf, 16, firmware_version)
    buf[18] = net_switch
    buf[19] = sub_switch
    struct.pack_into(">H", buf, 20, oem)
    buf[22] = 0  # UBEA version, n/a
    buf[23] = status1
    struct.pack_into("<H", buf, 24, esta_man)
    buf[26:44] = _pad_string(short_name, 18)
    buf[44:108] = _pad_string(long_name, 64)
    buf[108:172] = _pad_string(node_report, 64)

    num_ports = len(ports)
    struct.pack_into(">H", buf, 172, num_ports)

    for i, p in enumerate(ports):
        if not 0 <= p.universe_4bit <= 0x0F:
            raise ValueError(f"universe_4bit must be 0..15, got {p.universe_4bit}")
        port_type = PORT_TYPE_DMX512
        if p.is_input:
            port_type |= PORT_TYPE_INPUT
        if p.is_output:
            port_type |= PORT_TYPE_OUTPUT
        buf[174 + i] = port_type
        buf[178 + i] = 0x80 if p.is_input else 0x00   # GoodInput: bit 7 = data received
        buf[182 + i] = 0x80 if p.is_output else 0x00  # GoodOutputA: bit 7 = transmitting
        buf[186 + i] = p.universe_4bit  # SwIn
        buf[190 + i] = p.universe_4bit  # SwOut

    # buf[194] AcnPriority / SwVideo, buf[195] SwMacro, buf[196] SwRemote
    # buf[197:200] Spare1-3 zero
    buf[200] = 0x00  # Style: StNode
    buf[201:207] = mac
    buf[207:211] = bind_ip
    buf[211] = bind_index
    buf[212] = status2
    # buf[213:217] GoodOutputB per port, zero
    buf[217] = status3
    # buf[218:224] DefaultRespUID — zero (no RDM)
    # buf[224:239] Filler — zero

    return bytes(buf)


def _pad_string(s: str, length: int) -> bytes:
    """Encode as ASCII, truncate, right-pad with nulls. Final byte guaranteed null."""
    encoded = s.encode("ascii", errors="replace")[: length - 1]
    return encoded + b"\x00" * (length - len(encoded))


# Helpers for port-address packing
def port_address_components(port_address: int) -> tuple[int, int, int]:
    """Split a 15-bit port-address into (net, sub, universe). Net=7b, Sub=4b, Uni=4b."""
    if not 0 <= port_address <= PORT_ADDRESS_MAX:
        raise ValueError(f"port_address out of range: {port_address}")
    net = (port_address >> 8) & 0x7F
    sub = (port_address >> 4) & 0x0F
    uni = port_address & 0x0F
    return net, sub, uni


def port_address_from_components(net: int, sub: int, uni: int) -> int:
    """Combine (net, sub, universe) into a 15-bit port-address."""
    if not 0 <= net <= 0x7F:
        raise ValueError(f"net must be 0..127, got {net}")
    if not 0 <= sub <= 0x0F:
        raise ValueError(f"sub must be 0..15, got {sub}")
    if not 0 <= uni <= 0x0F:
        raise ValueError(f"uni must be 0..15, got {uni}")
    return (net << 8) | (sub << 4) | uni
