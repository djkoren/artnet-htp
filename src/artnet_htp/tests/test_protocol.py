"""Tests for protocol.py — pack/unpack round-trips and malformed-input rejection."""

from __future__ import annotations

import struct

import pytest

from artnet_htp.protocol import (
    ARTNET_MAGIC,
    ARTNET_PORT,
    OP_DMX,
    OP_POLL,
    OP_POLL_REPLY,
    PROT_VER_HI,
    PROT_VER_LO,
    ArtDmxPacket,
    PortInfo,
    build_artdmx,
    build_artpoll_reply,
    parse_artdmx,
    parse_artpoll,
    peek_opcode,
    port_address_components,
    port_address_from_components,
)


# ----------------------------- ArtDmx round-trip ----------------------------- #

class TestArtDmxRoundTrip:
    def test_basic_roundtrip(self):
        data = bytes((i & 0xFF) for i in range(512))
        pkt = build_artdmx(port_address=0, data=data, sequence=42, physical=1)
        parsed = parse_artdmx(pkt)
        assert parsed is not None
        assert parsed.port_address == 0
        assert parsed.sequence == 42
        assert parsed.physical == 1
        assert parsed.data == data

    def test_max_port_address(self):
        data = b"\x00" * 512
        pkt = build_artdmx(port_address=0x7FFF, data=data)
        parsed = parse_artdmx(pkt)
        assert parsed is not None
        assert parsed.port_address == 0x7FFF

    def test_short_dmx_data(self):
        data = bytes([1, 2, 3, 4])
        pkt = build_artdmx(port_address=1, data=data)
        parsed = parse_artdmx(pkt)
        assert parsed is not None
        assert parsed.data == data

    def test_port_address_split(self):
        # Net=5, Sub=10, Uni=3 → (5<<8) | (10<<4) | 3 = 0x5A3
        port = port_address_from_components(net=5, sub=10, uni=3)
        assert port == 0x5A3
        data = b"\xFF\xFF"
        pkt = build_artdmx(port_address=port, data=data)
        parsed = parse_artdmx(pkt)
        assert parsed is not None
        assert port_address_components(parsed.port_address) == (5, 10, 3)


# ----------------------------- ArtDmx malformed ------------------------------ #

class TestArtDmxMalformed:
    def _good_packet(self) -> bytes:
        return build_artdmx(port_address=0, data=b"\x00\x00")

    def test_too_short(self):
        assert parse_artdmx(b"") is None
        assert parse_artdmx(b"Art-Net") is None
        assert parse_artdmx(b"Art-Net\x00" + b"\x00" * 9) is None

    def test_wrong_magic(self):
        bad = bytearray(self._good_packet())
        bad[0] = ord("X")
        assert parse_artdmx(bytes(bad)) is None

    def test_wrong_opcode(self):
        bad = bytearray(self._good_packet())
        struct.pack_into("<H", bad, 8, OP_POLL)
        assert parse_artdmx(bytes(bad)) is None

    def test_old_protocol_version(self):
        bad = bytearray(self._good_packet())
        bad[11] = 13  # PROT_VER_LO must be >= 14
        assert parse_artdmx(bytes(bad)) is None

    def test_oversize_length(self):
        # build a packet by hand with length=600 (> 512) — must be rejected
        body = b"\x00" * 600
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_DMX)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0, 0, 0, 0])
            + struct.pack(">H", 600)
            + body
        )
        assert parse_artdmx(pkt) is None

    def test_odd_length(self):
        body = b"\x00" * 3
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_DMX)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0, 0, 0, 0])
            + struct.pack(">H", 3)
            + body
        )
        assert parse_artdmx(pkt) is None

    def test_length_zero(self):
        body = b""
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_DMX)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0, 0, 0, 0])
            + struct.pack(">H", 0)
            + body
        )
        assert parse_artdmx(pkt) is None

    def test_truncated_data(self):
        # claim length=512 but only provide 100 bytes
        body = b"\x00" * 100
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_DMX)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0, 0, 0, 0])
            + struct.pack(">H", 512)
            + body
        )
        assert parse_artdmx(pkt) is None

    def test_build_rejects_invalid(self):
        with pytest.raises(ValueError):
            build_artdmx(port_address=-1, data=b"\x00\x00")
        with pytest.raises(ValueError):
            build_artdmx(port_address=0x8000, data=b"\x00\x00")
        with pytest.raises(ValueError):
            build_artdmx(port_address=0, data=b"")
        with pytest.raises(ValueError):
            build_artdmx(port_address=0, data=b"\x00")  # odd
        with pytest.raises(ValueError):
            build_artdmx(port_address=0, data=b"\x00" * 514)  # too long


# ------------------------------- ArtPoll ------------------------------------ #

class TestArtPoll:
    def test_parse_artpoll(self):
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_POLL)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0x06, 0xDE])
        )
        parsed = parse_artpoll(pkt)
        assert parsed is not None
        assert parsed.talk_to_me == 0x06
        assert parsed.priority == 0xDE

    def test_artpoll_wrong_opcode(self):
        pkt = (
            ARTNET_MAGIC
            + struct.pack("<H", OP_DMX)
            + bytes([PROT_VER_HI, PROT_VER_LO, 0, 0])
        )
        assert parse_artpoll(pkt) is None

    def test_artpoll_too_short(self):
        assert parse_artpoll(b"Art-Net\x00") is None


# ----------------------------- ArtPollReply --------------------------------- #

class TestArtPollReply:
    def test_basic_build(self):
        reply = build_artpoll_reply(
            bind_ip=bytes([192, 168, 1, 50]),
            mac=bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x01]),
            short_name="HTP Merger",
            long_name="ArtNet HTP Merger (Pi)",
            node_report="OK - Ready",
            net_switch=0,
            sub_switch=0,
            ports=[
                PortInfo(universe_4bit=0, is_input=True, is_output=True),
                PortInfo(universe_4bit=1, is_input=True, is_output=True),
            ],
        )
        assert len(reply) == 239
        # magic + opcode
        assert reply[:8] == ARTNET_MAGIC
        assert struct.unpack_from("<H", reply, 8)[0] == OP_POLL_REPLY
        # IP + port
        assert reply[10:14] == bytes([192, 168, 1, 50])
        assert struct.unpack_from("<H", reply, 14)[0] == ARTNET_PORT
        # NumPorts
        assert struct.unpack_from(">H", reply, 172)[0] == 2
        # Port types: bit 7 (output) + bit 6 (input) | 0 (DMX512) = 0xC0
        assert reply[174] == 0xC0
        assert reply[175] == 0xC0
        assert reply[176] == 0x00  # no port 3
        # SwIn / SwOut
        assert reply[186] == 0
        assert reply[187] == 1
        assert reply[190] == 0
        assert reply[191] == 1
        # Style = StNode
        assert reply[200] == 0x00
        # MAC
        assert reply[201:207] == bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x01])
        # BindIp
        assert reply[207:211] == bytes([192, 168, 1, 50])

    def test_name_truncation(self):
        long = "x" * 200
        reply = build_artpoll_reply(
            bind_ip=b"\x00\x00\x00\x00",
            mac=b"\x00" * 6,
            short_name=long,
            long_name=long,
            node_report=long,
            net_switch=0,
            sub_switch=0,
            ports=[],
        )
        # short_name field is 18 bytes, final byte must be null
        assert reply[44 + 63] == 0
        assert reply[26 + 17] == 0
        assert reply[108 + 63] == 0

    def test_rejects_more_than_4_ports(self):
        with pytest.raises(ValueError):
            build_artpoll_reply(
                bind_ip=b"\x00\x00\x00\x00",
                mac=b"\x00" * 6,
                short_name="x",
                long_name="x",
                node_report="x",
                net_switch=0,
                sub_switch=0,
                ports=[PortInfo(0, True, True)] * 5,
            )

    def test_rejects_bad_ip_or_mac(self):
        with pytest.raises(ValueError):
            build_artpoll_reply(
                bind_ip=b"\x00",
                mac=b"\x00" * 6,
                short_name="x",
                long_name="x",
                node_report="x",
                net_switch=0,
                sub_switch=0,
                ports=[],
            )
        with pytest.raises(ValueError):
            build_artpoll_reply(
                bind_ip=b"\x00" * 4,
                mac=b"\x00",
                short_name="x",
                long_name="x",
                node_report="x",
                net_switch=0,
                sub_switch=0,
                ports=[],
            )


# ---------------------------- peek_opcode ----------------------------------- #

class TestPeekOpcode:
    def test_recognises_known(self):
        dmx = build_artdmx(0, b"\x00\x00")
        assert peek_opcode(dmx) == OP_DMX

    def test_returns_none_on_garbage(self):
        assert peek_opcode(b"") is None
        assert peek_opcode(b"XYZ") is None
        assert peek_opcode(b"Not-Net\x00" + b"\x00\x00") is None


# --------------------------- port-address helpers --------------------------- #

class TestPortAddressHelpers:
    def test_roundtrip(self):
        for pa in (0, 1, 0xFF, 0x100, 0x123, 0x7FFF):
            net, sub, uni = port_address_components(pa)
            assert port_address_from_components(net, sub, uni) == pa

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            port_address_components(-1)
        with pytest.raises(ValueError):
            port_address_components(0x8000)
        with pytest.raises(ValueError):
            port_address_from_components(net=128, sub=0, uni=0)
        with pytest.raises(ValueError):
            port_address_from_components(net=0, sub=16, uni=0)
        with pytest.raises(ValueError):
            port_address_from_components(net=0, sub=0, uni=16)
