"""ClaimKeepalive: keep the caller's claim alive during a long call (#2)."""

import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from labctl.core import claim_keepalive
from labctl.core.claim_keepalive import CLAIMED, LOST, ClaimKeepalive
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import ClaimActor, PlugType

ALICE = ClaimActor(session_id="s-alice", owner="alice")
BOB = ClaimActor(session_id="s-bob", owner="bob")


@pytest.fixture
def manager(tmp_path):
    m = ResourceManager(get_database(tmp_path / "lab.db"))
    m.create_sbc(name="pi-1")
    return m


def _claim(manager, actor=ALICE, duration=600, sbc="pi-1"):
    return manager.claim_sbc(
        sbc_name=sbc,
        agent_name=actor.owner,
        session_id=actor.session_id,
        session_kind="mcp-http",
        duration_seconds=duration,
        reason="testing",
        owner=actor.owner,
    )


def _age(manager, seconds):
    """Move the claim's deadline ``seconds`` into the past (relative to
    now), as if no call had touched it for that long."""
    when = manager._fmt_ts(datetime.now() - timedelta(seconds=seconds))
    manager.db.execute_modify(
        "UPDATE claims SET expires_at = ? WHERE released_at IS NULL", (when,)
    )


def _expires_at(manager):
    return manager.get_open_claim("pi-1").expires_at


def _db_down(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


class TestHeartbeatHeldClaim:
    """ResourceManager.heartbeat_held_claim: one conditional UPDATE."""

    def test_heartbeats_the_holder(self, manager):
        _claim(manager)
        _age(manager, -10)
        before = _expires_at(manager)
        assert manager.heartbeat_held_claim("pi-1", ALICE) is True
        after = _expires_at(manager)
        assert after > before + timedelta(seconds=500)
        # Same timestamp format as the rest of the claims table.
        row = manager.db.execute_one("SELECT expires_at FROM claims")
        assert len(row["expires_at"]) == len("2026-01-01 00:00:00.000000")

    def test_same_owner_from_another_session(self, manager):
        _claim(manager)
        actor = ClaimActor(session_id="other", owner="alice")
        assert manager.heartbeat_held_claim("pi-1", actor) is True

    def test_not_for_someone_else(self, manager):
        _claim(manager)
        before = _expires_at(manager)
        assert manager.heartbeat_held_claim("pi-1", BOB) is False
        assert manager.heartbeat_held_claim("pi-1", ClaimActor("x")) is False
        assert _expires_at(manager) == before

    def test_not_for_an_expired_or_released_claim(self, manager):
        _claim(manager)
        _age(manager, 5)
        assert manager.heartbeat_held_claim("pi-1", ALICE) is False
        assert manager.get_active_claim("pi-1") is None

    def test_heartbeat_claim_uses_it(self, manager):
        _claim(manager)
        assert manager.heartbeat_claim("pi-1", "s-alice") is True
        assert manager.heartbeat_claim("pi-1", "s-bob") is False

    def test_touch_claim_reports_a_zero_row_update(self, manager):
        claim = _claim(manager)
        assert manager.touch_claim(claim) is True
        manager.force_release_claim("pi-1", reason="r")
        assert manager.touch_claim(claim) is False


class TestBeat:
    def test_heartbeats_the_holders_claim(self, manager):
        _claim(manager)
        _age(manager, -10)  # 10 s left
        before = _expires_at(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            assert ka.held
            assert ka.beat() is True
        finally:
            ka.stop()
        assert _expires_at(manager) > before + timedelta(seconds=500)
        assert ka.beats == 1

    def test_no_claim_means_nothing_to_heartbeat(self, manager):
        ka = ClaimKeepalive(manager, "pi-1", ALICE).start()
        try:
            assert not ka.held
            assert ka._thread is None
            assert ka.beat() is True
            assert ka.beats == 0
        finally:
            ka.stop()

    def test_someone_elses_claim_is_never_heartbeated(self, manager):
        _claim(manager, BOB)
        _age(manager, -10)
        before = _expires_at(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            assert not ka.held
            assert ka.beat() is True
        assert _expires_at(manager) == before

    def test_never_creates_a_claim(self, manager):
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            ka.beat()
            ka.checkpoint()
        assert manager.get_open_claim("pi-1") is None

    def test_disabled_does_nothing(self, manager):
        _claim(manager)
        _age(manager, -10)
        before = _expires_at(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, enabled=False) as ka:
            assert not ka.held
            assert ka.checkpoint() is None
        assert _expires_at(manager) == before

    def test_released_claim_is_lost(self, manager):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            manager.force_release_claim("pi-1", reason="needed it")
            assert ka.beat() is False
            assert "no longer held" in ka.lost_reason
            assert ka.lost_reason.kind == LOST
            # Lost stays lost, even if the caller claims it again.
            _claim(manager)
            assert ka.beat() is False
        finally:
            ka.stop()

    def test_expired_claim_is_lost_and_not_revived(self, manager):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            _age(manager, 5)
            assert ka.beat() is False
        finally:
            ka.stop()
        assert manager.get_active_claim("pi-1") is None

    def test_zero_row_update_is_lost(self, manager, monkeypatch):
        """The claim looked held, but the UPDATE changed nothing."""
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            monkeypatch.setattr(manager.db, "execute_modify", lambda *a: 0)
            assert ka.beat() is False
            assert ka.lost_reason.kind == LOST
        finally:
            ka.stop()

    def test_lost_to_another_holder_names_them(self, manager):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            manager.force_release_claim("pi-1", reason="r")
            _claim(manager, BOB)
            assert ka.beat() is False
            assert "now claimed by 'bob'" in ka.lost_reason
        finally:
            ka.stop()

    def test_database_error_is_a_missed_beat(self, manager, monkeypatch):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            monkeypatch.setattr(manager, "heartbeat_held_claim", _db_down)
            assert ka.beat() is True
            assert ka.lost_reason is None
        finally:
            ka.stop()

    def test_database_error_past_the_deadline_is_lost(self, manager, monkeypatch):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:
            monkeypatch.setattr(manager, "heartbeat_held_claim", _db_down)
            ka._deadline = 0.0  # the claim's time ran out without a beat
            assert ka.beat() is False
            assert "couldn't be renewed" in ka.lost_reason
        finally:
            ka.stop()


class TestStart:
    def test_database_error_carries_on_without_keepalive(
        self, manager, monkeypatch, caplog
    ):
        _claim(manager)
        monkeypatch.setattr(manager, "get_open_claim", _db_down)
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            assert not ka.held
            assert ka._thread is None
        assert "carrying on without keeping it alive" in caplog.text


class TestThread:
    def _watch(self, manager):
        beat = threading.Event()
        real = manager.heartbeat_held_claim

        def heartbeat(sbc, actor):
            result = real(sbc, actor)
            beat.set()
            return result

        manager.heartbeat_held_claim = heartbeat
        return beat

    def test_beats_in_the_background_and_stops(self, manager):
        _claim(manager)
        beat = self._watch(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=0.01)
        with ka:
            assert beat.wait(5)
        assert ka._thread is None
        count = ka.beats
        beat.clear()
        assert not beat.wait(0.1)
        assert ka.beats == count

    def test_stops_when_the_operation_raises(self, manager):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600)
        with pytest.raises(ValueError):
            with ka:
                assert ka._thread is not None
                raise ValueError("boom")
        assert ka._thread is None

    def test_thread_ends_once_the_claim_is_lost(self, manager):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=0.01).start()
        manager.force_release_claim("pi-1", reason="r")
        ka._thread.join(5)
        assert not ka._thread.is_alive()
        assert ka.lost_reason
        ka.stop()

    def test_interval_is_a_quarter_of_the_duration(self, manager):
        _claim(manager, duration=120)
        ka = ClaimKeepalive(manager, "pi-1", ALICE)
        assert ka.next_interval() == 30.0

    def test_interval_follows_a_renew_to_a_shorter_duration(self, manager):
        _claim(manager, duration=3600)
        ka = ClaimKeepalive(manager, "pi-1", ALICE)
        assert ka.next_interval() == claim_keepalive.MAX_INTERVAL_SECONDS
        manager.renew_claim("pi-1", "s-alice", duration_seconds=60, owner="alice")
        assert ka.next_interval() == 15.0

    def test_interval_bounds(self, manager):
        _claim(manager, duration=1)
        ka = ClaimKeepalive(manager, "pi-1", ALICE)
        assert ka.next_interval() == claim_keepalive.MIN_INTERVAL_SECONDS


class TestCheckpoint:
    def test_holder_goes_on_and_is_heartbeated(self, manager):
        _claim(manager)
        _age(manager, -10)
        before = _expires_at(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            assert ka.checkpoint() is None
        assert _expires_at(manager) > before

    def test_lost_claim_stops(self, manager):
        _claim(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            manager.force_release_claim("pi-1", reason="r")
            reason = ka.checkpoint()
        assert "no longer held" in reason and reason.kind == LOST

    def test_own_claim_taken_over_counts_as_lost(self, manager):
        _claim(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            manager.force_release_claim("pi-1", reason="r")
            _claim(manager, BOB)
            reason = ka.checkpoint()
        assert reason.kind == LOST
        assert "now claimed by 'bob'" in reason

    def test_new_claim_by_someone_else_stops_an_unclaimed_run(self, manager):
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            assert ka.checkpoint() is None
            _claim(manager, BOB)
            reason = ka.checkpoint()
        assert "now claimed by 'bob'" in reason and reason.kind == CLAIMED

    def _share(self, manager):
        pi1 = manager.get_sbc_by_name("pi-1")
        pi2 = manager.create_sbc(name="pi-2")
        manager.assign_power_plug(pi1.id, PlugType.TASMOTA, "pdu", 1)
        manager.assign_power_plug(pi2.id, PlugType.TASMOTA, "pdu", 1)

    def test_claim_on_a_board_sharing_hardware_stops(self, manager):
        self._share(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            _claim(manager, BOB, sbc="pi-2")
            reason = ka.checkpoint()
        assert "'pi-2' is now claimed by 'bob'" in reason
        assert reason.kind == CLAIMED

    def test_sharer_claimed_does_not_heartbeat_own_claim(self, manager):
        """The recheck runs first: a refused checkpoint extends nothing."""
        self._share(manager)
        _claim(manager)
        _age(manager, -10)
        before = _expires_at(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            _claim(manager, BOB, sbc="pi-2")
            assert ka.checkpoint().kind == CLAIMED
        assert _expires_at(manager) == before

    def test_overridden_claims_are_allowed_new_ones_are_not(self, manager):
        self._share(manager)
        theirs = _claim(manager, BOB)
        with ClaimKeepalive(
            manager, "pi-1", ALICE, allowed_claim_ids={theirs.id}
        ) as ka:
            assert ka.checkpoint() is None
            _claim(manager, ClaimActor("s-carol", "carol"), sbc="pi-2")
            assert "'pi-2' is now claimed by 'carol'" in ka.checkpoint()

    def test_recheck_off_ignores_others_claims(self, manager):
        _claim(manager, BOB)
        with ClaimKeepalive(manager, "pi-1", ALICE, recheck=False) as ka:
            assert ka.checkpoint() is None

    def test_require_claim_stops_without_one(self, manager):
        with ClaimKeepalive(manager, "pi-1", ALICE, require_claim=True) as ka:
            assert "must be claimed first" in ka.checkpoint()

    def test_unchanged_without_claims(self, manager):
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            assert ka.checkpoint() is None
            assert ka.checkpoint() is None

    def test_database_error_goes_on(self, manager, monkeypatch):
        _claim(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            monkeypatch.setattr(manager, "check_claim", _db_down)
            monkeypatch.setattr(manager, "heartbeat_held_claim", _db_down)
            assert ka.checkpoint() is None

    def test_database_error_without_a_claim_goes_on(self, manager, monkeypatch):
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            monkeypatch.setattr(manager, "sbcs_sharing_hardware_with", _db_down)
            assert ka.checkpoint() is None

    def test_database_error_past_the_deadline_stops(self, manager, monkeypatch):
        _claim(manager)
        with ClaimKeepalive(manager, "pi-1", ALICE, interval=3600) as ka:
            monkeypatch.setattr(manager, "check_claim", _db_down)
            ka._deadline = 0.0
            reason = ka.checkpoint()
        assert reason.kind == LOST and "database error" in reason
