"""Stored timestamps are parsed in one place, labctl.core.timeutil (#65)."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from labctl.core import timeutil
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import Status

SRC = Path(__file__).resolve().parents[2] / "src" / "labctl"
UTC = timezone.utc

# Shapes a stored or API-given timestamp may take (all 12:00:00.5 UTC
# except the ones without a fraction).
SHAPES = [
    "{d} 12:00:00",
    "{d} 12:00:00.5",
    "{d} 12:00:00.500",
    "{d} 12:00:00.500000",
    "{d}T12:00:00.5Z",
    "{d}T12:00:00.5000+0000",
    "{d}T04:00:00.500000-08:00",
]


@pytest.fixture
def manager(tmp_path):
    return ResourceManager(get_database(tmp_path / "labctl.db"))


@pytest.mark.parametrize("shape", SHAPES)
def test_status_fast_power_age(shape, monkeypatch):
    from labctl.cli import _cached_status_power_states

    now = datetime(2026, 11, 1, 12, 1, 0, tzinfo=UTC)
    monkeypatch.setattr("labctl.core.timeutil.now", lambda: now)
    sbc = SimpleNamespace(
        name="pi",
        power_plug=object(),
        last_power_state="on",
        last_power_at=shape.format(d="2026-11-01"),
    )
    out = _cached_status_power_states([sbc], "", stale_threshold=3600)
    assert "ON" in out["pi"]  # parsed (not "-"), and fresh


@pytest.mark.parametrize("shape", SHAPES)
def test_uptime_with_any_stored_shape(manager, shape, monkeypatch):
    sbc = manager.create_sbc(name="pi")
    manager.update_sbc(sbc.id, status=Status.ONLINE)
    manager.db.execute_modify("DELETE FROM status_log")
    manager.db.execute_insert(
        "INSERT INTO status_log (sbc_id, status, logged_at) VALUES (?, 'online', ?)",
        (sbc.id, shape.format(d="2026-11-01")),
    )
    now = datetime(2026, 11, 1, 13, 0, 0, tzinfo=UTC)
    monkeypatch.setattr("labctl.core.timeutil.now", lambda: now)
    uptime = manager.get_uptime(sbc.id)
    assert 3599 <= uptime["current_uptime_seconds"] <= 3600


def test_status_history_carries_utc_offset(manager):
    sbc = manager.create_sbc(name="pi")
    manager.log_status(sbc.id, Status.ONLINE)
    (entry,) = manager.get_status_history(sbc.id)
    assert entry["logged_at"].endswith("Z")
    assert timeutil.from_db(entry["logged_at"]) <= timeutil.now() + timedelta(seconds=1)


def test_no_other_stored_timestamp_parser():
    """strptime/fromisoformat on stored timestamps only in timeutil and the
    v14 migration (which reads old local times on purpose); services.py
    parses systemd's output, not ours."""
    allowed = {"core/timeutil.py", "core/database.py", "services.py"}
    found = []
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC))
        if rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in (
                "strptime",
                "fromisoformat",
            ):
                found.append(f"{rel}:{node.lineno}")
    assert found == []
