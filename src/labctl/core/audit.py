"""
Activity stream — structured record of every state-changing action.

Each call to `emit()` writes one row to `audit_log`. The `actor`,
`source`, and `claim_id` fields are pulled from contextvars set by
the originating front-end (CLI, MCP, web, daemon) via the
`activity_context()` context manager.

See docs/SPEC_activity_stream.md for the full design.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    from labctl.core.database import Database

logger = logging.getLogger(__name__)

DETAILS_MAX_BYTES = 4096
SERIAL_BUFFER_HEAD_TAIL = 256
REDACT_KEYS = frozenset(
    {
        "password",
        "token",
        "api_key",
        "apikey",
        "ssh_key",
        "session_cookie",
        "cookie",
        "authorization",
    }
)
REDACT_PLACEHOLDER = "***"


_actor: ContextVar[str] = ContextVar("labctl_audit_actor", default="internal")
_source: ContextVar[str] = ContextVar("labctl_audit_source", default="internal")
_claim_id: ContextVar[Optional[int]] = ContextVar(
    "labctl_audit_claim_id", default=None
)


@contextmanager
def activity_context(
    actor: str,
    source: str,
    claim_id: Optional[int] = None,
) -> Iterator[None]:
    """Set the audit context for the enclosed block.

    Usage at a front-end boundary:

        with activity_context(f"cli:{getuser()}", "cli"):
            main()
    """
    token_a = _actor.set(actor)
    token_s = _source.set(source)
    token_c = _claim_id.set(claim_id)
    try:
        yield
    finally:
        _actor.reset(token_a)
        _source.reset(token_s)
        _claim_id.reset(token_c)


def set_context(
    actor: str,
    source: str,
    claim_id: Optional[int] = None,
) -> None:
    """Set the audit context for the remainder of this contextvars context.

    Prefer `activity_context()` for scoped changes. Use this at process
    entry (e.g., `main()` in the CLI) where there's no natural exit
    point to pair with.
    """
    _actor.set(actor)
    _source.set(source)
    _claim_id.set(claim_id)


def current_actor() -> str:
    return _actor.get()


def current_source() -> str:
    return _source.get()


def current_claim_id() -> Optional[int]:
    return _claim_id.get()


def _redact(obj: Any) -> Any:
    """Recursively strip sensitive fields from a details payload."""
    if isinstance(obj, dict):
        return {
            k: REDACT_PLACEHOLDER if k.lower() in REDACT_KEYS else _redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return _truncate_buffer(bytes(obj))
    return obj


def _truncate_buffer(buf: bytes) -> str:
    """Truncate large binary buffers to head+tail with elision marker."""
    if len(buf) <= 2 * SERIAL_BUFFER_HEAD_TAIL:
        return buf.decode("utf-8", errors="replace")
    head = buf[:SERIAL_BUFFER_HEAD_TAIL].decode("utf-8", errors="replace")
    tail = buf[-SERIAL_BUFFER_HEAD_TAIL:].decode("utf-8", errors="replace")
    elided = len(buf) - 2 * SERIAL_BUFFER_HEAD_TAIL
    return f"{head}...<{elided} bytes elided>...{tail}"


def _serialize_details(details: Optional[dict]) -> Optional[str]:
    if details is None:
        return None
    redacted = _redact(details)
    text = json.dumps(redacted, default=str, separators=(",", ":"))
    if len(text.encode("utf-8")) > DETAILS_MAX_BYTES:
        trimmed = text.encode("utf-8")[: DETAILS_MAX_BYTES - 32]
        text = trimmed.decode("utf-8", errors="replace") + '..."_truncated":true}'
    return text


def _now_ms() -> str:
    """ISO8601 timestamp with millisecond precision."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"


def row_to_event_dict(row) -> dict[str, Any]:
    """Convert an audit_log row to the public event representation."""
    return {
        "id": row["id"],
        "logged_at": row["logged_at"],
        "actor": row["actor"] or "internal",
        "source": row["source"] or "internal",
        "action": row["action"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "entity_name": row["entity_name"],
        "result": row["result"] or "ok",
        "details": row["details"],
        "claim_id": row["claim_id"],
    }


def query_events(
    db: "Database",
    *,
    limit: int = 50,
    sbc: Optional[str] = None,
    actor: Optional[str] = None,
    source: Optional[str] = None,
    result: Optional[str] = None,
    since: Optional[str] = None,
    after_id: Optional[int] = None,
    order_desc: bool = True,
) -> list[dict[str, Any]]:
    """Query audit_log rows with the standard activity filters."""
    where: list[str] = []
    params: list[Any] = []
    for value, column in (
        (sbc, "entity_name"),
        (actor, "actor"),
        (source, "source"),
        (result, "result"),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if since:
        where.append("logged_at >= ?")
        params.append(since)
    if after_id is not None:
        where.append("id > ?")
        params.append(int(after_id))

    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC" if order_desc else " ORDER BY id ASC"
    sql += " LIMIT ?"
    params.append(int(limit))
    return [row_to_event_dict(r) for r in db.execute(sql, tuple(params))]


def prune_old_events(db: "Database", *, older_than_days: int = 30) -> int:
    """Delete audit events older than the given retention window."""
    cutoff = (datetime.now() - timedelta(days=int(older_than_days))).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]
    return db.execute_modify(
        """
        DELETE FROM audit_log
        WHERE logged_at < ?
        """,
        (cutoff,),
    )


def emit(
    db: "Database",
    *,
    action: str,
    entity_type: str = "",
    entity_id: Optional[int] = None,
    entity_name: Optional[str] = None,
    result: str = "ok",
    details: Optional[dict] = None,
    actor: Optional[str] = None,
    source: Optional[str] = None,
    claim_id: Optional[int] = None,
) -> None:
    """Record a single activity event.

    Args:
        db: Database handle.
        action: Canonical verb_noun action name (e.g. "power_on").
        entity_type: Kind of entity acted on (e.g. "sbc", "serial_port").
        entity_id: Numeric ID of the entity (optional).
        entity_name: Human-readable target (e.g. SBC name). Used as `target`.
        result: "ok" | "error" | "forbidden".
        details: Structured details. Redacted and JSON-encoded.
        actor: Override the contextvar actor (rare; usually leave None).
        source: Override the contextvar source.
        claim_id: Override the contextvar claim id.

    Never raises — audit failures are logged but do not propagate.
    """
    try:
        effective_actor = actor if actor is not None else _actor.get()
        effective_source = source if source is not None else _source.get()
        effective_claim = claim_id if claim_id is not None else _claim_id.get()
        details_text = _serialize_details(details)
        db.execute_insert(
            """
            INSERT INTO audit_log (
                action, entity_type, entity_id, entity_name,
                details, logged_at, actor, source, result, claim_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                entity_type,
                entity_id,
                entity_name,
                details_text,
                _now_ms(),
                effective_actor,
                effective_source,
                result,
                effective_claim,
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit.emit failed for action=%s: %s", action, e)
