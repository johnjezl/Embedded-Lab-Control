"""Unit tests for the activity-stream broadcaster (Phase B)."""

import json
import queue
import threading
import time

import pytest

from labctl.core import audit
from labctl.core.database import Database
from labctl.web.activity_broadcaster import (
    ActivityBroadcaster,
    ActivityEvent,
    iter_sse_frames,
)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.initialize()
    return d


@pytest.fixture
def broadcaster(db):
    # Fast polling for tests.
    b = ActivityBroadcaster(db, poll_interval=0.05)
    b.start()
    yield b
    b.stop()


def _wait_for(q: queue.Queue, timeout: float = 2.0) -> ActivityEvent:
    return q.get(timeout=timeout)


def _drain(q: queue.Queue) -> list[ActivityEvent]:
    out = []
    try:
        while True:
            out.append(q.get_nowait())
    except queue.Empty:
        pass
    return out


class TestActivityEvent:
    def test_sse_frame_format(self):
        evt = ActivityEvent(
            id=42,
            logged_at="2026-04-19 10:00:00.123",
            actor="cli:john",
            source="cli",
            action="power_on",
            entity_type="sbc",
            entity_id=1,
            entity_name="pi-5-1",
            result="ok",
            details=None,
            claim_id=None,
        )
        frame = evt.to_sse()
        assert frame.startswith("id: 42\n")
        assert "event: activity\n" in frame
        assert "data: {" in frame
        assert frame.endswith("\n\n")
        data_line = next(
            ln for ln in frame.splitlines() if ln.startswith("data: ")
        )
        parsed = json.loads(data_line[len("data: ") :])
        assert parsed["id"] == 42
        assert parsed["actor"] == "cli:john"
        assert parsed["action"] == "power_on"


class TestBroadcaster:
    def test_start_is_idempotent(self, db):
        b = ActivityBroadcaster(db, poll_interval=0.05)
        b.start()
        first_thread = b._thread
        b.start()
        assert b._thread is first_thread
        b.stop()

    def test_no_replay_of_pre_start_history(self, db):
        """Events written before start() must not be replayed."""
        audit.emit(db, action="old1", entity_type="sbc", entity_name="x")
        audit.emit(db, action="old2", entity_type="sbc", entity_name="x")

        b = ActivityBroadcaster(db, poll_interval=0.05)
        b.start()
        try:
            q = b.subscribe()
            time.sleep(0.2)  # let poll cycle a few times
            assert _drain(q) == []
        finally:
            b.stop()

    def test_new_event_reaches_subscriber(self, broadcaster, db):
        q = broadcaster.subscribe()
        try:
            audit.emit(
                db, action="power_on", entity_type="sbc", entity_name="pi-5-1"
            )
            evt = _wait_for(q)
            assert evt.action == "power_on"
            assert evt.entity_name == "pi-5-1"
            assert evt.result == "ok"
        finally:
            broadcaster.unsubscribe(q)

    def test_fan_out_to_multiple_subscribers(self, broadcaster, db):
        q1 = broadcaster.subscribe()
        q2 = broadcaster.subscribe()
        try:
            audit.emit(db, action="power_off", entity_type="sbc", entity_name="x")
            e1 = _wait_for(q1)
            e2 = _wait_for(q2)
            assert e1.id == e2.id
            assert e1.action == "power_off"
        finally:
            broadcaster.unsubscribe(q1)
            broadcaster.unsubscribe(q2)

    def test_unsubscribe_stops_delivery(self, broadcaster, db):
        q = broadcaster.subscribe()
        audit.emit(db, action="a", entity_type="sbc", entity_name="x")
        _wait_for(q)
        broadcaster.unsubscribe(q)
        audit.emit(db, action="b", entity_type="sbc", entity_name="x")
        time.sleep(0.2)
        assert _drain(q) == []

    def test_recent_backfill(self, broadcaster, db):
        audit.emit(db, action="evt1", entity_type="sbc", entity_name="a")
        audit.emit(db, action="evt2", entity_type="sbc", entity_name="b")
        recent = broadcaster.recent(limit=10)
        actions = [e.action for e in recent]
        # Oldest first.
        assert actions == ["evt1", "evt2"]

    def test_slow_consumer_is_drained_not_blocking(self, db):
        """A full subscriber queue must not stall the broadcaster."""
        b = ActivityBroadcaster(db, poll_interval=0.05)
        b.start()
        try:
            # Force an artificially tiny queue for this subscriber.
            q = queue.Queue(maxsize=2)
            with b._subscribers_lock:
                b._subscribers.add(q)
            try:
                for i in range(10):
                    audit.emit(db, action=f"a{i}", entity_type="sbc", entity_name="x")
                # Give time to fan out.
                time.sleep(0.3)
                # After drop-on-full, queue should have been emptied.
                # The broadcaster should still be alive and responsive.
                assert b._thread.is_alive()
            finally:
                with b._subscribers_lock:
                    b._subscribers.discard(q)
        finally:
            b.stop()


class TestSSEStream:
    def test_iter_sse_frames_sends_hydration_then_live(self, broadcaster, db):
        audit.emit(db, action="hydrated", entity_type="sbc", entity_name="x")
        # Wait for broadcaster to seed last_id past the hydration event.
        time.sleep(0.15)

        frames_iter = iter_sse_frames(broadcaster, backfill=10, heartbeat_seconds=999)

        # First frame = retry preamble.
        retry_frame = next(frames_iter)
        assert retry_frame.startswith("retry:")

        # Next = hydration event(s). Walk until we see the hydrated action.
        found_hydrated = False
        for _ in range(5):
            frame = next(frames_iter)
            if "hydrated" in frame:
                found_hydrated = True
                break
        assert found_hydrated, "hydration event missing from SSE stream"

        # Live event flows through.
        result_holder: dict = {}

        def pull_live():
            result_holder["frame"] = next(frames_iter)

        t = threading.Thread(target=pull_live, daemon=True)
        t.start()
        time.sleep(0.1)
        audit.emit(db, action="live_event", entity_type="sbc", entity_name="y")
        t.join(timeout=2.0)
        assert "frame" in result_holder
        assert "live_event" in result_holder["frame"]
