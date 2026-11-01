"""
Timestamps: stored in UTC, shown in local time (#63).

Everything labctl writes to the database is UTC, in the text layout
SQLite's ``CURRENT_TIMESTAMP`` uses (``YYYY-MM-DD HH:MM:SS[.ffffff]``), so
values written by Python and by SQLite compare and sort together. In
Python they are timezone-aware UTC datetimes; machine output (REST, MCP,
NDJSON) carries the offset, and people see local time with the zone
abbreviation, which keeps the repeated hour at a DST change unambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc
DB_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def now() -> datetime:
    """The current time, aware UTC."""
    return datetime.now(UTC)


def to_db(dt: datetime) -> str:
    """``dt`` as stored: UTC, microseconds, no offset. A naive ``dt`` is
    taken to be UTC already."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime(DB_FORMAT)


def from_db(value) -> Optional[datetime]:
    """A stored timestamp (or an ISO-8601 one, with or without an offset or
    ``Z``) as aware UTC; None for None or text that isn't a timestamp. A
    value without an offset is UTC (as everything stored is)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """``dt`` for machines: ISO-8601 UTC with ``Z``."""
    if dt is None:
        return None
    return from_db(dt).isoformat().replace("+00:00", "Z")


def local(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """``dt`` for people: local time with the zone abbreviation."""
    if dt is None:
        return "?"
    return from_db(dt).astimezone().strftime(fmt)


def db_iso(value) -> Optional[str]:
    """A stored timestamp as :func:`iso` text (unchanged if unparseable)."""
    dt = from_db(value)
    return iso(dt) if dt is not None else value


def db_local(value, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """A stored timestamp as :func:`local` text (unchanged if unparseable)."""
    dt = from_db(value)
    return local(dt, fmt) if dt is not None else ("" if value is None else str(value))
