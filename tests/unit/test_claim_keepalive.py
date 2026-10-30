"""ClaimKeepalive: keep the caller's claim alive during a long call (#2)."""

import threading
from datetime import datetime, timedelta

import pytest

from labctl.core.claim_keepalive import ClaimKeepalive
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import ClaimActor

ALICE = ClaimActor(session_id="s-alice", owner="alice")
BOB = ClaimActor(session_id="s-bob", owner="bob")


@pytest.fixture
def manager(tmp_path):
    m = ResourceManager(get_database(tmp_path / "lab.db"))
    m.create_sbc(name="pi-1")
    return m


def _claim(manager, actor=ALICE, duration=600):
    return manager.claim_sbc(
        sbc_name="pi-1",
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

    def test_database_error_is_not_a_lost_claim(self, manager, monkeypatch):
        _claim(manager)
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=3600).start()
        try:

            def broken(name):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(manager, "get_active_claim", broken)
            assert ka.beat() is True
            assert ka.lost_reason is None
        finally:
            ka.stop()


class TestThread:
    def test_beats_in_the_background_and_stops(self, manager):
        _claim(manager)
        beat = threading.Event()
        ka = ClaimKeepalive(manager, "pi-1", ALICE, interval=0.01)
        real_touch = manager.touch_claim

        def touch(claim):
            real_touch(claim)
            beat.set()

        manager.touch_claim = touch
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

    def test_interval_follows_the_claim_duration(self, manager):
        _claim(manager, duration=120)
        ka = ClaimKeepalive(manager, "pi-1", ALICE)
        seen = {}
        real = threading.Thread

        def fake_thread(target, args, **kw):
            seen["interval"] = args[0]
            return real(target=lambda *a: None, args=args, **kw)

        import labctl.core.claim_keepalive as mod

        orig = mod.threading.Thread
        mod.threading.Thread = fake_thread
        try:
            ka.start()
        finally:
            mod.threading.Thread = orig
            ka.stop()
        assert seen["interval"] == 30.0  # a quarter of 120 s


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
            assert "no longer held" in ka.checkpoint()

    def test_new_claim_by_someone_else_stops_an_unclaimed_run(self, manager):
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            assert ka.checkpoint() is None
            _claim(manager, BOB)
            assert "now claimed by 'bob'" in ka.checkpoint()

    def test_claim_on_a_board_sharing_hardware_stops(self, manager):
        from labctl.core.models import PlugType

        pi1 = manager.get_sbc_by_name("pi-1")
        pi2 = manager.create_sbc(name="pi-2")
        manager.assign_power_plug(pi1.id, PlugType.TASMOTA, "pdu", 1)
        manager.assign_power_plug(pi2.id, PlugType.TASMOTA, "pdu", 1)
        with ClaimKeepalive(manager, "pi-1", ALICE) as ka:
            manager.claim_sbc(
                sbc_name="pi-2",
                agent_name="bob",
                session_id="s-bob",
                session_kind="mcp-http",
                duration_seconds=600,
                reason="r",
                owner="bob",
            )
            assert "'pi-2' is now claimed by 'bob'" in ka.checkpoint()

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
