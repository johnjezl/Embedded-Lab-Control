"""Unit tests for the activity-stream audit module (Phase A)."""

import json
import sqlite3
from datetime import datetime

import pytest

from labctl.core import audit
from labctl.core.database import SCHEMA_VERSION, Database
from labctl.core.manager import ResourceManager


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.initialize()
    return d


@pytest.fixture(autouse=True)
def reset_audit_context():
    audit.set_context("internal", "internal", claim_id=None)
    yield
    audit.set_context("internal", "internal", claim_id=None)


@pytest.fixture
def manager(db):
    return ResourceManager(db)


def _all_events(db: Database):
    return [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id")]


class TestSchemaV5:
    def test_fresh_db_has_audit_columns(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(audit_log)")}
        for expected in ("actor", "source", "result", "claim_id"):
            assert expected in cols, f"Missing column {expected}"

    def test_migration_from_v4(self, tmp_path):
        """v4 databases should be upgraded to v5 with new columns."""
        path = tmp_path / "v4.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                entity_name TEXT,
                details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE claims (id INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (4);
            INSERT INTO audit_log (action, entity_type, entity_name, details)
                VALUES ('legacy_action', 'sbc', 'legacy-sbc', 'legacy row');
            """
        )
        conn.commit()
        conn.close()

        d = Database(path)
        d.initialize()

        cols = {r["name"] for r in d.execute("PRAGMA table_info(audit_log)")}
        for expected in ("actor", "source", "result", "claim_id"):
            assert expected in cols

        legacy = d.execute_one(
            "SELECT * FROM audit_log WHERE action = ?", ("legacy_action",)
        )
        assert legacy["actor"] == "internal"
        assert legacy["source"] == "internal"
        assert legacy["result"] == "ok"
        assert legacy["claim_id"] is None


class TestEmit:
    def test_emit_writes_row(self, db):
        audit.emit(
            db,
            action="power_on",
            entity_type="sbc",
            entity_id=1,
            entity_name="pi-5-1",
            result="ok",
            details={"duration_ms": 42},
        )
        events = _all_events(db)
        assert len(events) == 1
        e = events[0]
        assert e["action"] == "power_on"
        assert e["entity_name"] == "pi-5-1"
        assert e["result"] == "ok"
        assert json.loads(e["details"]) == {"duration_ms": 42}

    def test_emit_defaults_to_internal_actor(self, db):
        audit.emit(db, action="x", entity_type="sbc", entity_name="y")
        e = _all_events(db)[0]
        assert e["actor"] == "internal"
        assert e["source"] == "internal"

    def test_emit_failure_does_not_propagate(self, db, monkeypatch):
        """audit failures must never crash the caller."""
        def boom(*args, **kwargs):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(db, "execute_insert", boom)
        # Must not raise.
        audit.emit(db, action="anything", entity_type="sbc")

    def test_emit_writes_millisecond_timestamp(self, db):
        audit.emit(db, action="x", entity_type="sbc")
        e = _all_events(db)[0]
        # Format: "YYYY-MM-DD HH:MM:SS.mmm"
        assert "." in e["logged_at"]
        ms = e["logged_at"].rsplit(".", 1)[1]
        assert len(ms) == 3 and ms.isdigit()


class TestContext:
    def test_activity_context_sets_and_resets(self, db):
        with audit.activity_context("cli:alice", "cli"):
            audit.emit(db, action="a", entity_type="sbc", entity_name="inside")
        # After exit the defaults should be restored.
        audit.emit(db, action="b", entity_type="sbc", entity_name="after")

        events = _all_events(db)
        assert events[0]["actor"] == "cli:alice"
        assert events[0]["source"] == "cli"
        assert events[1]["actor"] == "internal"
        assert events[1]["source"] == "internal"

    def test_claim_id_propagates_from_context(self, db, manager):
        """With a real claim row, claim_id flows through emit."""
        sbc = manager.create_sbc(name="claim-target", project="t")
        claim = manager.claim_sbc(
            sbc_name="claim-target",
            agent_name="test-agent",
            session_id="test-sess",
            session_kind="cli",
            reason="testing",
            duration_seconds=60,
        )
        with audit.activity_context("cli:alice", "cli", claim_id=claim.id):
            audit.emit(db, action="a", entity_type="sbc", entity_name="claim-target")
        # First activity_context emit is the one we care about; grab by action.
        evt = db.execute_one(
            "SELECT * FROM audit_log WHERE action = 'a'"
        )
        assert evt["claim_id"] == claim.id

    def test_nested_contexts(self, db):
        with audit.activity_context("outer", "mcp"):
            audit.emit(db, action="outer_event", entity_type="sbc")
            with audit.activity_context("inner", "cli"):
                audit.emit(db, action="inner_event", entity_type="sbc")
            audit.emit(db, action="after_inner", entity_type="sbc")

        events = _all_events(db)
        assert [e["actor"] for e in events] == ["outer", "inner", "outer"]
        assert [e["source"] for e in events] == ["mcp", "cli", "mcp"]

    def test_set_context_no_reset(self, db):
        """set_context is fire-and-forget — used at process entry."""
        audit.set_context("cli:bob", "cli")
        audit.emit(db, action="x", entity_type="sbc")
        assert _all_events(db)[0]["actor"] == "cli:bob"

    def test_emit_kwargs_override_context(self, db):
        with audit.activity_context("ctx-actor", "cli"):
            audit.emit(
                db,
                action="x",
                entity_type="sbc",
                actor="override",
                source="mcp",
            )
        e = _all_events(db)[0]
        assert e["actor"] == "override"
        assert e["source"] == "mcp"


class TestRedaction:
    def test_password_and_token_fields_are_redacted(self, db):
        audit.emit(
            db,
            action="x",
            entity_type="sbc",
            details={
                "password": "hunter2",
                "api_key": "sk-abc",
                "token": "jwt...",
                "ssh_key": "-----BEGIN...",
                "duration_ms": 10,
            },
        )
        d = json.loads(_all_events(db)[0]["details"])
        assert d["password"] == "***"
        assert d["api_key"] == "***"
        assert d["token"] == "***"
        assert d["ssh_key"] == "***"
        assert d["duration_ms"] == 10

    def test_redaction_is_case_insensitive(self, db):
        audit.emit(
            db,
            action="x",
            entity_type="sbc",
            details={"PASSWORD": "secret", "Authorization": "Bearer x"},
        )
        d = json.loads(_all_events(db)[0]["details"])
        assert d["PASSWORD"] == "***"
        assert d["Authorization"] == "***"

    def test_redaction_recurses_into_nested_dicts(self, db):
        audit.emit(
            db,
            action="x",
            entity_type="sbc",
            details={"nested": {"password": "secret", "ok": 1}},
        )
        d = json.loads(_all_events(db)[0]["details"])
        assert d["nested"]["password"] == "***"
        assert d["nested"]["ok"] == 1

    def test_large_buffer_truncated(self, db):
        payload = b"A" * 2000
        audit.emit(
            db, action="serial_send", entity_type="sbc", details={"buf": payload}
        )
        d = json.loads(_all_events(db)[0]["details"])
        assert "<1488 bytes elided>" in d["buf"]

    def test_details_field_truncated_to_4kb(self, db):
        huge = {"payload": "X" * 10000}
        audit.emit(db, action="x", entity_type="sbc", details=huge)
        raw = _all_events(db)[0]["details"]
        assert len(raw.encode("utf-8")) <= audit.DETAILS_MAX_BYTES


class TestManagerIntegration:
    """Existing _audit_log callers continue to work and now record actor/source."""

    def test_create_sbc_records_context_actor(self, manager, db):
        with audit.activity_context("cli:john", "cli"):
            manager.create_sbc(name="integration-sbc", project="X")
        events = [e for e in _all_events(db) if e["action"] == "create"]
        assert len(events) == 1
        assert events[0]["actor"] == "cli:john"
        assert events[0]["source"] == "cli"
        assert events[0]["entity_name"] == "integration-sbc"
        assert events[0]["result"] == "ok"


class TestQueryAndPrune:
    def test_query_events_filters_and_ordering(self, db):
        with audit.activity_context("cli:alice", "cli"):
            audit.emit(db, action="first", entity_type="sbc", entity_name="pi-a")
        with audit.activity_context("mcp-1", "mcp"):
            audit.emit(db, action="second", entity_type="sbc", entity_name="pi-b")

        events = audit.query_events(db, actor="cli:alice", order_desc=False)
        assert len(events) == 1
        assert events[0]["action"] == "first"
        assert events[0]["actor"] == "cli:alice"

        all_desc = audit.query_events(db, limit=10)
        assert [e["action"] for e in all_desc] == ["second", "first"]

    def test_prune_old_events(self, db):
        db.execute_insert(
            """
            INSERT INTO audit_log (
                action, entity_type, entity_name, details, logged_at,
                actor, source, result, claim_id
            ) VALUES (?, ?, ?, ?, datetime('now', '-40 days'), ?, ?, ?, ?)
            """,
            ("old", "sbc", "old-pi", None, "internal", "internal", "ok", None),
        )
        audit.emit(db, action="new", entity_type="sbc", entity_name="new-pi")

        pruned = audit.prune_old_events(db, older_than_days=30)
        assert pruned == 1

        events = audit.query_events(db, limit=10, order_desc=False)
        assert len(events) == 1
        assert events[0]["action"] == "new"

    def test_prune_uses_local_wall_clock_cutoff(self, db, monkeypatch):
        class FixedDateTime:
            @staticmethod
            def now():
                return datetime(2026, 4, 20, 0, 30, 0)

        monkeypatch.setattr(audit, "datetime", FixedDateTime)
        db.execute_insert(
            """
            INSERT INTO audit_log (
                action, entity_type, entity_name, details, logged_at,
                actor, source, result, claim_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "boundary",
                "sbc",
                "boundary-pi",
                None,
                "2026-03-21 01:00:00.000",
                "internal",
                "internal",
                "ok",
                None,
            ),
        )

        pruned = audit.prune_old_events(db, older_than_days=30)
        assert pruned == 0
        events = audit.query_events(db, limit=10)
        assert len(events) == 1
        assert events[0]["action"] == "boundary"


# ---------------------------------------------------------------------------
# Schema v6 + power-cache plumbing for `labctl status --fast`
# ---------------------------------------------------------------------------


class TestSchemaV6:
    def test_schema_version_is_6(self):
        from labctl.core.database import SCHEMA_VERSION

        assert SCHEMA_VERSION == 6

    def test_fresh_db_has_power_cache_columns(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(sbcs)")}
        assert "last_power_state" in cols
        assert "last_power_at" in cols

    def test_migration_from_v5_to_v6(self, tmp_path):
        """A pre-v6 database must gain the new columns and be readable."""
        path = tmp_path / "v5.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE sbcs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                project TEXT,
                description TEXT,
                ssh_user TEXT DEFAULT 'root',
                status TEXT DEFAULT 'unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                entity_name TEXT,
                details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actor TEXT NOT NULL DEFAULT 'internal',
                source TEXT NOT NULL DEFAULT 'internal',
                result TEXT NOT NULL DEFAULT 'ok',
                claim_id INTEGER
            );
            CREATE TABLE claims (id INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (5);
            INSERT INTO sbcs (name, project) VALUES ('legacy-sbc', 'p');
            """
        )
        conn.commit()
        conn.close()

        from labctl.core.database import Database

        Database(path).initialize()

        d2 = Database(path)
        cols = {r["name"] for r in d2.execute("PRAGMA table_info(sbcs)")}
        assert "last_power_state" in cols
        assert "last_power_at" in cols

        # Pre-existing rows survive with NULL columns.
        row = d2.execute_one("SELECT * FROM sbcs WHERE name = ?", ("legacy-sbc",))
        assert row["last_power_state"] is None
        assert row["last_power_at"] is None


class TestPowerObservationPersistence:
    def test_update_power_observation_writes_state_and_timestamp(self, manager):
        sbc = manager.create_sbc(name="cache-sbc", project="x")
        manager.update_power_observation(sbc.id, "on")

        fresh = manager.get_sbc(sbc.id)
        assert fresh.last_power_state == "on"
        assert fresh.last_power_at is not None

    def test_update_power_observation_normalizes_unknown(self, manager):
        sbc = manager.create_sbc(name="cache-unknown", project="x")
        manager.update_power_observation(sbc.id, None)
        fresh = manager.get_sbc(sbc.id)
        assert fresh.last_power_state == "unknown"

        manager.update_power_observation(sbc.id, "garbage")
        fresh = manager.get_sbc(sbc.id)
        assert fresh.last_power_state == "unknown"

    def test_update_stamps_timestamp_each_call(self, manager):
        """`last_power_at` must move forward even when the value is unchanged."""
        import time

        sbc = manager.create_sbc(name="cache-bump", project="x")
        manager.update_power_observation(sbc.id, "on")
        first = manager.get_sbc(sbc.id).last_power_at

        time.sleep(1.1)  # SQLite CURRENT_TIMESTAMP has 1-second resolution
        manager.update_power_observation(sbc.id, "on")
        second = manager.get_sbc(sbc.id).last_power_at

        assert second != first  # advanced
