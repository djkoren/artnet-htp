"""Tests for HTP merge logic and MergerState."""

from __future__ import annotations

import pytest

from artnet_htp.state import (
    MergerState,
    OutputSpec,
    SourceSpec,
    htp_merge,
    priority_merge,
    tiered_merge,
)


# --------------------------- pure htp_merge --------------------------------- #

class TestHTP:
    def test_empty(self):
        assert htp_merge([]) == bytes(512)

    def test_single(self):
        a = bytes([100, 50, 200]) + bytes(509)
        assert htp_merge([a]) == a

    def test_two_sources(self):
        a = bytes([100, 50, 200, 0]) + bytes(508)
        b = bytes([50, 200, 100, 255]) + bytes(508)
        merged = htp_merge([a, b])
        assert merged[:4] == bytes([100, 200, 200, 255])
        assert merged[4:] == bytes(508)

    def test_three_sources(self):
        a = bytes([10, 0, 0]) + bytes(509)
        b = bytes([0, 20, 0]) + bytes(509)
        c = bytes([0, 0, 30]) + bytes(509)
        merged = htp_merge([a, b, c])
        assert merged[:3] == bytes([10, 20, 30])

    def test_identity_with_zero(self):
        a = bytes([100, 100, 100]) + bytes(509)
        z = bytes(512)
        assert htp_merge([a, z]) == a

    def test_idempotent_identical(self):
        a = bytes([100, 100, 100]) + bytes(509)
        assert htp_merge([a, a, a, a]) == a


# --------------------------- pure priority_merge --------------------------- #

class TestPriorityMerge:
    """priority_merge: highest priority NUMBER wins (e.g. 200 > 100 > 50)."""

    def test_empty(self):
        assert priority_merge([]) == bytes(512)

    def test_single_active(self):
        a = bytes([100, 0, 0]) + bytes(509)
        assert priority_merge([(a, 100)]) == a

    def test_single_all_zero_returns_zeros(self):
        a = bytes(512)
        assert priority_merge([(a, 100)]) == bytes(512)

    def test_higher_priority_number_wins_exclusively(self):
        # FPP at priority 200 (higher number), Madrix at priority 100.
        fpp = bytes([50, 50, 0]) + bytes(509)
        madrix = bytes([200, 0, 200]) + bytes(509)
        # FPP wins entirely — even on channels where Madrix has higher VALUES.
        assert priority_merge([(fpp, 200), (madrix, 100)]) == fpp

    def test_falls_through_when_winner_blackedout(self):
        fpp_blackout = bytes(512)
        madrix = bytes([100, 0, 100]) + bytes(509)
        # FPP (higher priority) is blacked out → Madrix takes over
        assert priority_merge([(fpp_blackout, 200), (madrix, 100)]) == madrix

    def test_input_order_does_not_matter(self):
        fpp = bytes([200]) + bytes(511)
        madrix = bytes([100]) + bytes(511)
        # FPP wins regardless of list order
        assert priority_merge([(madrix, 100), (fpp, 200)]) == fpp
        assert priority_merge([(fpp, 200), (madrix, 100)]) == fpp

    def test_all_blacked_out_returns_zeros(self):
        a = bytes(512)
        b = bytes(512)
        assert priority_merge([(a, 100), (b, 200)]) == bytes(512)


# --------------------------- pure tiered_merge ----------------------------- #

class TestTieredMerge:
    """Two-tier: priority-mode sources override htp-mode sources."""

    def test_empty(self):
        assert tiered_merge([]) == bytes(512)

    def test_only_htp_sources(self):
        a = bytes([100, 0]) + bytes(510)
        b = bytes([0, 200]) + bytes(510)
        # Both htp → htp merge (channel-wise max)
        merged = tiered_merge([(a, "htp", 100), (b, "htp", 100)])
        assert merged[:2] == bytes([100, 200])

    def test_priority_overrides_htp(self):
        fpp = bytes([50, 50, 50]) + bytes(509)
        madrix = bytes([200, 200, 200]) + bytes(509)
        other = bytes([100, 100, 100]) + bytes(509)
        # FPP is priority mode → overrides everything else even if its values are lower
        merged = tiered_merge([
            (fpp, "priority", 100),
            (madrix, "htp", 100),
            (other, "htp", 100),
        ])
        assert merged[:3] == bytes([50, 50, 50])

    def test_priority_blackout_falls_to_htp(self):
        fpp_blackout = bytes(512)
        madrix = bytes([100, 0]) + bytes(510)
        other = bytes([0, 200]) + bytes(510)
        merged = tiered_merge([
            (fpp_blackout, "priority", 100),
            (madrix, "htp", 100),
            (other, "htp", 100),
        ])
        # FPP silent → falls through to HTP of madrix + other
        assert merged[:2] == bytes([100, 200])

    def test_multiple_priority_sources_higher_number_wins(self):
        a = bytes([100]) + bytes(511)
        b = bytes([200]) + bytes(511)
        # Both priority mode, a=priority 50, b=priority 100 → b wins
        merged = tiered_merge([
            (a, "priority", 50),
            (b, "priority", 100),
        ])
        assert merged[0] == 200

    def test_priority_active_blocks_htp_even_when_other_priority_blacked(self):
        active_priority = bytes([10]) + bytes(511)
        blackout_priority = bytes(512)
        htp_source = bytes([255]) + bytes(511)
        # active_priority wins, htp_source ignored
        merged = tiered_merge([
            (active_priority, "priority", 100),
            (blackout_priority, "priority", 50),
            (htp_source, "htp", 100),
        ])
        assert merged[0] == 10  # priority source wins despite low value


# --------------------------- MergerState integration ----------------------- #

class TestMergerState:
    def _state(self, **overrides) -> MergerState:
        timeout = overrides.pop("source_timeout_s", 2.5)
        keepalive = overrides.pop("send_keepalive_when_silent", True)
        auto_allow = overrides.pop("auto_allow_unknown_sources", False)
        s = MergerState(
            source_timeout_s=timeout,
            send_keepalive_when_silent=keepalive,
            auto_allow_unknown_sources=auto_allow,
        )
        s.apply_config(
            sources=overrides.pop("sources",
                                  [SourceSpec("10.0.0.1"), SourceSpec("10.0.0.2")]),
            outputs=overrides.pop("outputs", [OutputSpec("10.0.0.100")]),
            universes=overrides.pop("universes", [0, 1]),
            source_timeout_s=timeout,
            send_keepalive_when_silent=keepalive,
            auto_allow_unknown_sources=auto_allow,
        )
        return s

    def test_unknown_source_rejected(self):
        s = self._state()
        accepted = s.update_source("99.99.99.99", 0, bytes([200] * 512), now=1.0)
        assert accepted is False

    def test_unknown_source_tracked(self):
        s = self._state()
        s.mark_unknown_source("99.99.99.99", now=1.0)
        s.mark_unknown_source("99.99.99.99", now=1.2)
        snap = s.snapshot(now=2.0)
        unknown = {u["ip"]: u for u in snap["unknown_sources"]}
        assert "99.99.99.99" in unknown
        assert unknown["99.99.99.99"]["packet_count"] == 2

    def test_universe_filter(self):
        s = self._state()
        # universe 5 is not configured
        assert s.update_source("10.0.0.1", 5, bytes(512), now=1.0) is False

    def test_basic_merge(self):
        s = self._state()
        a = bytes([100, 50]) + bytes(510)
        b = bytes([50, 200]) + bytes(510)
        s.update_source("10.0.0.1", 0, a, now=1.0)
        s.update_source("10.0.0.2", 0, b, now=1.0)
        merged = s.compute_merge(port_address=0, now=1.1)
        assert merged is not None
        assert merged[:2] == bytes([100, 200])

    def test_source_timeout_drops_contribution(self):
        s = self._state(source_timeout_s=2.5)
        a = bytes([255]) + bytes(511)   # source 1 at full
        b = bytes([100]) + bytes(511)   # source 2 at half
        s.update_source("10.0.0.1", 0, a, now=0.0)
        s.update_source("10.0.0.2", 0, b, now=0.0)

        # both fresh
        merged = s.compute_merge(0, now=1.0)
        assert merged[0] == 255

        # source 1 goes silent, source 2 keeps sending
        s.update_source("10.0.0.2", 0, b, now=2.0)
        # 1.0 -> 4.0 = 4s since source 1 last seen; > 2.5s timeout
        merged = s.compute_merge(0, now=4.0)
        assert merged[0] == 100  # source 1 dropped, source 2 takes over

    def test_all_silent_returns_zeros_with_keepalive(self):
        s = self._state(send_keepalive_when_silent=True)
        merged = s.compute_merge(0, now=1.0)
        assert merged == bytes(512)

    def test_all_silent_returns_none_without_keepalive(self):
        s = self._state(send_keepalive_when_silent=False)
        merged = s.compute_merge(0, now=1.0)
        assert merged is None

    def test_silent_then_revive(self):
        s = self._state(source_timeout_s=2.5)
        a = bytes([200]) + bytes(511)
        s.update_source("10.0.0.1", 0, a, now=0.0)
        # 5s later, source dead
        merged = s.compute_merge(0, now=5.0)
        assert merged == bytes(512)  # all zeros with keepalive on
        # Source comes back
        s.update_source("10.0.0.1", 0, a, now=5.5)
        merged = s.compute_merge(0, now=5.6)
        assert merged[0] == 200

    def test_short_dmx_zero_padded(self):
        s = self._state()
        s.update_source("10.0.0.1", 0, bytes([255, 255]), now=0.0)
        merged = s.compute_merge(0, now=0.1)
        assert merged[:2] == bytes([255, 255])
        assert merged[2:] == bytes(510)

    def test_sequence_wraps(self):
        s = self._state()
        last = 0
        for _ in range(260):
            seq = s.bump_sequence("10.0.0.100", 0)
            assert 1 <= seq <= 255
            last = seq
        # After 260 bumps starting from 0 → 1..255..1..(wrapped a few times)
        # Confirm wrap behavior: 260 bumps = 255 + 5 → counter at 5
        assert last == 5

    def test_per_output_sequence_independent(self):
        s = self._state(outputs=[OutputSpec("10.0.0.100"), OutputSpec("10.0.0.200")])
        seq_a = s.bump_sequence("10.0.0.100", 0)
        seq_b = s.bump_sequence("10.0.0.200", 0)
        seq_a2 = s.bump_sequence("10.0.0.100", 0)
        assert seq_a == 1
        assert seq_b == 1
        assert seq_a2 == 2

    def test_per_universe_sequence_independent(self):
        s = self._state()
        for _ in range(3):
            s.bump_sequence("10.0.0.100", 0)
        seq_uni1 = s.bump_sequence("10.0.0.100", 1)
        assert seq_uni1 == 1

    def test_apply_config_evicts_removed_source(self):
        s = self._state()
        s.update_source("10.0.0.1", 0, bytes([200] * 512), now=0.0)
        s.apply_config(
            sources=[SourceSpec("10.0.0.2")],
            outputs=[OutputSpec("10.0.0.100")],
            universes=[0],
            source_timeout_s=2.5,
            send_keepalive_when_silent=True,
            auto_allow_unknown_sources=False,
        )
        # 10.0.0.1 dropped → merge should now be zeros
        merged = s.compute_merge(0, now=0.1)
        assert merged == bytes(512)

    def test_apply_config_evicts_removed_universe(self):
        s = self._state()
        s.update_source("10.0.0.1", 0, bytes([200] * 512), now=0.0)
        s.apply_config(
            sources=[SourceSpec("10.0.0.1")],
            outputs=[OutputSpec("10.0.0.100")],
            universes=[1],  # 0 removed
            source_timeout_s=2.5,
            send_keepalive_when_silent=True,
            auto_allow_unknown_sources=False,
        )
        # Universe 0 no longer configured → compute_merge returns None
        assert s.compute_merge(0, now=0.1) is None

    def test_auto_allow_unknown(self):
        s = self._state(auto_allow_unknown_sources=True)
        s.apply_config(
            sources=[],  # no allowlist
            outputs=[OutputSpec("10.0.0.100")],
            universes=[0],
            source_timeout_s=2.5,
            send_keepalive_when_silent=True,
            auto_allow_unknown_sources=True,
        )
        accepted = s.update_source("172.16.0.5", 0, bytes([100] * 512), now=0.0)
        assert accepted is True
        merged = s.compute_merge(0, now=0.1)
        assert merged[0] == 100

    def test_allow_source_promotes_unknown(self):
        s = self._state()
        s.mark_unknown_source("10.0.0.99", now=0.0)
        assert any(u["ip"] == "10.0.0.99" for u in s.snapshot()["unknown_sources"])
        s.allow_source("10.0.0.99", label="newly trusted")
        assert not any(u["ip"] == "10.0.0.99" for u in s.snapshot()["unknown_sources"])
        # Now packets from it are accepted
        assert s.update_source("10.0.0.99", 0, bytes([42] * 512), now=1.0) is True

    def test_block_source_removes_data(self):
        s = self._state()
        s.update_source("10.0.0.1", 0, bytes([200] * 512), now=0.0)
        s.update_source("10.0.0.2", 0, bytes([50] * 512), now=0.0)
        merged = s.compute_merge(0, now=0.1)
        assert merged[0] == 200
        s.block_source("10.0.0.1")
        merged = s.compute_merge(0, now=0.2)
        assert merged[0] == 50

    # ----- per-source mode integration -----
    def test_priority_source_overrides_htp_sources(self):
        # Madrix htp; FPP priority. Both sending. FPP wins entirely.
        s = self._state(sources=[
            SourceSpec("10.0.0.1", label="Madrix", mode="htp", priority=100),
            SourceSpec("10.0.0.2", label="FPP", mode="priority", priority=100),
        ])
        madrix_data = bytes([200, 200, 200]) + bytes(509)
        fpp_data = bytes([50, 50, 50]) + bytes(509)
        # Madrix alone (FPP not yet sending) → Madrix wins (it's the only contributor)
        s.update_source("10.0.0.1", 0, madrix_data, now=0.0)
        assert s.compute_merge(0, now=0.1)[:3] == bytes([200, 200, 200])
        # FPP joins → FPP overrides
        s.update_source("10.0.0.2", 0, fpp_data, now=0.0)
        assert s.compute_merge(0, now=0.1)[:3] == bytes([50, 50, 50])

    def test_priority_source_blackout_falls_to_htp(self):
        s = self._state(sources=[
            SourceSpec("10.0.0.1", label="Madrix", mode="htp"),
            SourceSpec("10.0.0.2", label="FPP", mode="priority"),
        ])
        s.update_source("10.0.0.1", 0, bytes([200]) + bytes(511), now=0.0)
        s.update_source("10.0.0.2", 0, bytes(512), now=0.0)  # FPP blackout
        merged = s.compute_merge(0, now=0.1)
        assert merged[0] == 200  # Madrix wins because FPP has nothing

    def test_two_htp_sources_merge_channel_max(self):
        s = self._state(sources=[
            SourceSpec("10.0.0.1", mode="htp"),
            SourceSpec("10.0.0.2", mode="htp"),
        ])
        s.update_source("10.0.0.1", 0, bytes([0, 200]) + bytes(510), now=0.0)
        s.update_source("10.0.0.2", 0, bytes([100, 0]) + bytes(510), now=0.0)
        merged = s.compute_merge(0, now=0.1)
        assert merged[:2] == bytes([100, 200])  # HTP

    def test_multiple_priority_sources_highest_number_wins(self):
        s = self._state(sources=[
            SourceSpec("10.0.0.1", mode="priority", priority=50),
            SourceSpec("10.0.0.2", mode="priority", priority=200),
        ])
        s.update_source("10.0.0.1", 0, bytes([100]) + bytes(511), now=0.0)
        s.update_source("10.0.0.2", 0, bytes([50]) + bytes(511), now=0.0)
        merged = s.compute_merge(0, now=0.1)
        assert merged[0] == 50  # priority 200 wins over priority 50

    def test_snapshot_includes_active_flag(self):
        s = self._state()
        # source 1 active (non-zero), source 2 alive but blackout (zeros)
        s.update_source("10.0.0.1", 0, bytes([100, 50]) + bytes(510), now=0.0)
        s.update_source("10.0.0.2", 0, bytes(512), now=0.0)
        snap = s.snapshot(now=0.1)
        by_ip = {x["ip"]: x for x in snap["sources"]}
        assert by_ip["10.0.0.1"]["alive"] is True
        assert by_ip["10.0.0.1"]["active"] is True
        assert by_ip["10.0.0.2"]["alive"] is True
        assert by_ip["10.0.0.2"]["active"] is False

    def test_snapshot_shape(self):
        s = self._state()
        s.update_source("10.0.0.1", 0, bytes([100] * 512), now=0.0)
        s.update_source("10.0.0.1", 1, bytes([200] * 512), now=0.0)
        s.record_send("10.0.0.100", count=5)
        snap = s.snapshot(now=0.1)
        sources = {x["ip"]: x for x in snap["sources"]}
        assert sources["10.0.0.1"]["alive"] is True
        assert sources["10.0.0.1"]["packet_count"] == 2
        assert len(sources["10.0.0.1"]["universes"]) == 2
        # Source 2 is configured but never seen
        assert sources["10.0.0.2"]["alive"] is False
        outputs = {x["ip"]: x for x in snap["outputs"]}
        assert outputs["10.0.0.100"]["packet_count"] == 5
        assert snap["universes"] == [0, 1]
