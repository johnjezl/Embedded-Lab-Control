"""
Activity-stream broadcaster for the web server.

A single background thread polls `audit_log` for new rows and fans
them out to per-subscriber queues. SSE endpoints iterate their queue
to stream events to clients.

Polling the DB (rather than hooking `audit.emit()` directly) means
events from CLI, MCP, and the monitor daemon — which run in separate
processes — all reach the browser.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from labctl.core.database import Database

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5
BATCH_LIMIT = 500
SUBSCRIBER_QUEUE_MAXSIZE = 1000
INITIAL_BACKFILL = 200  # Hydration events a new subscriber receives immediately.


@dataclass
class ActivityEvent:
    """One event pushed to subscribers."""

    id: int
    logged_at: str
    actor: str
    source: str
    action: str
    entity_type: str
    entity_id: Optional[int]
    entity_name: Optional[str]
    result: str
    details: Optional[str]  # JSON string as stored
    claim_id: Optional[int]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "logged_at": self.logged_at,
            "actor": self.actor,
            "source": self.source,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "result": self.result,
            "details": self.details,
            "claim_id": self.claim_id,
        }

    def to_sse(self) -> str:
        """Render as a single SSE frame."""
        payload = json.dumps(self.to_dict(), separators=(",", ":"))
        return f"id: {self.id}\nevent: activity\ndata: {payload}\n\n"


def _row_to_event(row) -> ActivityEvent:
    return ActivityEvent(
        id=row["id"],
        logged_at=row["logged_at"] or "",
        actor=row["actor"] or "internal",
        source=row["source"] or "internal",
        action=row["action"] or "",
        entity_type=row["entity_type"] or "",
        entity_id=row["entity_id"],
        entity_name=row["entity_name"],
        result=row["result"] or "ok",
        details=row["details"],
        claim_id=row["claim_id"],
    )


class ActivityBroadcaster:
    """Singleton broadcaster: polls the audit_log and feeds subscriber queues."""

    def __init__(self, db: Database, poll_interval: float = POLL_INTERVAL_SECONDS):
        self.db = db
        self.poll_interval = poll_interval
        self._last_id: int = 0
        self._subscribers: set[queue.Queue] = set()
        self._subscribers_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background poll thread. Safe to call multiple times."""
        if self._thread and self._thread.is_alive():
            return
        # Seed the cursor with the current max id so we don't replay history.
        row = self.db.execute_one("SELECT COALESCE(MAX(id), 0) AS m FROM audit_log")
        self._last_id = row["m"] if row else 0
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="activity-broadcaster", daemon=True
        )
        self._thread.start()
        logger.info(
            "ActivityBroadcaster started (last_id=%d, interval=%.2fs)",
            self._last_id,
            self.poll_interval,
        )

    def stop(self) -> None:
        """Signal the poll thread to stop. Does not join."""
        self._stop_event.set()

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber queue. Call unsubscribe() when done."""
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        with self._subscribers_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subscribers_lock:
            self._subscribers.discard(q)

    def recent(self, limit: int = INITIAL_BACKFILL) -> list[ActivityEvent]:
        """Return the most recent N events (oldest-first for replay)."""
        rows = self.db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        return [_row_to_event(r) for r in reversed(rows)]

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("ActivityBroadcaster poll failed: %s", e)
            self._stop_event.wait(self.poll_interval)

    def _poll_once(self) -> None:
        rows = self.db.execute(
            "SELECT * FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT ?",
            (self._last_id, BATCH_LIMIT),
        )
        if not rows:
            return
        events = [_row_to_event(r) for r in rows]
        self._last_id = events[-1].id
        self._fanout(events)

    def _fanout(self, events: list[ActivityEvent]) -> None:
        """Push events to every subscriber queue. Drop slow consumers."""
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            for event in events:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # Slow consumer: drop all events to catch up rather than
                    # block others. The client will still have its initial
                    # backfill + whatever lands next.
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    logger.info(
                        "ActivityBroadcaster: dropped slow subscriber queue"
                    )
                    break  # don't keep adding to a drained queue this tick


def iter_sse_frames(
    broadcaster: ActivityBroadcaster,
    backfill: int = INITIAL_BACKFILL,
    heartbeat_seconds: float = 15.0,
) -> Iterator[str]:
    """Yield SSE frames: first a hydration batch, then live events.

    Sends a comment-only heartbeat line every `heartbeat_seconds` so
    intermediaries (nginx, browsers) don't drop an idle connection.
    """
    q = broadcaster.subscribe()
    try:
        yield "retry: 2000\n\n"
        for event in broadcaster.recent(backfill):
            yield event.to_sse()
        last_beat = time.monotonic()
        while True:
            timeout = max(0.0, heartbeat_seconds - (time.monotonic() - last_beat))
            try:
                event = q.get(timeout=timeout or 0.1)
                yield event.to_sse()
            except queue.Empty:
                yield ": keepalive\n\n"
                last_beat = time.monotonic()
    finally:
        broadcaster.unsubscribe(q)
