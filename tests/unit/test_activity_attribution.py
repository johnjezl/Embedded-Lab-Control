"""Who the activity stream says acted (#57): daemons, MCP agents, and
status observations kept out of it."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.core import audit
from labctl.core.database import SCHEMA_VERSION, Database
from labctl.core.manager import ResourceManager
from labctl.core.models import Status


@pytest.fixture(autouse=True)
def reset_audit_context():
    audit.set_context("internal", "internal")
    yield
    audit.set_context("internal", "internal")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.initialize()
    return d


def _events(db):
    return [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id")]


class TestAgent:
    def test_context_agent_recorded(self, db):
        with audit.activity_context("mcp-stdio:1-2", "mcp", agent="fw-bot"):
            audit.emit(db, action="power_on", entity_type="sbc", entity_name="pi")
        assert _events(db)[-1]["agent"] == "fw-bot"

    def test_set_agent_applies_to_rest_of_call(self, db):
        with audit.activity_context("mcp-stdio:1-2", "mcp"):
            audit.emit(db, action="a", entity_type="sbc")
            audit.set_agent("named-now")
            audit.emit(db, action="b", entity_type="sbc")
        audit.emit(db, action="c", entity_type="sbc")  # outside: reset
        assert [e["agent"] for e in _events(db)] == [None, "named-now", None]

    def test_query_filter_and_event_dict(self, db):
        with audit.activity_context("mcp-stdio:1-2", "mcp", agent="a1"):
            audit.emit(db, action="x", entity_type="sbc")
        with audit.activity_context("mcp-stdio:3-4", "mcp", agent="a2"):
            audit.emit(db, action="y", entity_type="sbc")
        events = audit.query_events(db, agent="a2")
        assert [(e["action"], e["agent"]) for e in events] == [("y", "a2")]


class TestSchemaV13:
    def test_version(self):
        assert SCHEMA_VERSION == 13

    def test_v12_upgraded_keeps_rows(self, tmp_path):
        path = tmp_path / "labctl.db"
        Database(path).initialize()
        conn = sqlite3.connect(path)
        conn.executescript("""
            DROP INDEX idx_audit_log_agent;
            ALTER TABLE audit_log DROP COLUMN agent;
            INSERT INTO audit_log (action, entity_type) VALUES ('claim', 'sbc');
            DELETE FROM schema_version;
            INSERT INTO schema_version (version) VALUES (12);
            """)
        conn.commit()
        conn.close()

        db = Database(path)
        db.initialize()
        cols = {r["name"] for r in db.execute("PRAGMA table_info(audit_log)")}
        assert "agent" in cols
        rows = db.execute("SELECT action, agent FROM audit_log")
        assert [(r["action"], r["agent"]) for r in rows] == [("claim", None)]
        indexes = {
            r["name"]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'audit_log'"
            )
        }
        assert "idx_audit_log_agent" in indexes


class TestStatusIsNotAnAction:
    def test_record_status_logs_history_not_activity(self, db):
        manager = ResourceManager(db)
        sbc = manager.create_sbc(name="pi")
        before = len(_events(db))

        manager.record_status(sbc.id, Status.ONLINE, "ping ok")

        assert manager.get_sbc(sbc.id).status == Status.ONLINE
        history = manager.get_status_history(sbc.id)
        assert history[0]["status"] == "online"
        assert len(_events(db)) == before

    def test_daemon_status_change_not_in_activity(self, db):
        from labctl.health.checks import HealthCheckSummary
        from labctl.health.daemon import MonitorDaemon

        manager = ResourceManager(db)
        manager.create_sbc(name="pi")
        before = len(_events(db))
        daemon = MonitorDaemon(manager, MagicMock(), MagicMock())
        summary = HealthCheckSummary(sbc_name="pi")
        summary.recommended_status = Status.OFFLINE

        daemon._process_result("pi", summary)

        assert manager.get_sbc_by_name("pi").status == Status.OFFLINE
        assert len(_events(db)) == before

    def test_editing_an_sbc_is_still_recorded(self, db):
        manager = ResourceManager(db)
        sbc = manager.create_sbc(name="pi")
        manager.update_sbc(sbc.id, description="bench 2")
        assert _events(db)[-1]["action"] == "update"


class TestDaemonActor:
    def test_monitor_runs_as_daemon(self, tmp_path):
        from labctl.cli import main

        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        seen = {}

        def fake_start(self):
            seen["actor"] = audit.current_actor()
            seen["source"] = audit.current_source()

        with patch("labctl.health.MonitorDaemon.start", fake_start):
            result = CliRunner().invoke(
                main, ["-c", str(config), "monitor", "--no-alerts"]
            )
        assert result.exit_code == 0, result.output
        assert seen == {"actor": "daemon:monitor", "source": "daemon"}

    def test_mcp_sweep_releases_as_daemon(self, db):
        pytest.importorskip("mcp")
        from labctl import mcp_server
        from labctl.core.config import Config

        manager = ResourceManager(db)
        manager.create_sbc(name="pi")
        manager.claim_sbc(
            sbc_name="pi",
            agent_name="a",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=1,
            reason="r",
        )
        db.execute_modify("UPDATE claims SET expires_at = '2000-01-01 00:00:00.000000'")
        config = Config()
        config.claims.grace_period_seconds = 0
        with (
            patch.object(mcp_server, "_get_manager", return_value=manager),
            patch.object(mcp_server, "_get_config", return_value=config),
        ):
            mcp_server._sweep_claims_once()

        expired = [e for e in _events(db) if e["action"] == "claim_expired"]
        assert expired and expired[-1]["actor"] == "daemon:mcp"
        assert expired[-1]["source"] == "daemon"
        assert "Claim by a expired" in expired[-1]["details"]
        assert audit.current_actor() == "internal"  # scoped to the sweep


class TestCliAgentFilter:
    def _setup(self, tmp_path):
        config = tmp_path / "config.yaml"
        dbp = tmp_path / "labctl.db"
        config.write_text(f"database_path: {dbp}\n")
        db = Database(dbp)
        db.initialize()
        with audit.activity_context("mcp-stdio:1-2", "mcp", agent="fw-bot"):
            audit.emit(db, action="power_on", entity_type="sbc", entity_name="pi")
        with audit.activity_context("cli:john", "cli"):
            audit.emit(db, action="power_off", entity_type="sbc", entity_name="pi")
        return config

    def test_tail_shows_and_filters_agent(self, tmp_path):
        from labctl.cli import main

        config = self._setup(tmp_path)
        result = CliRunner().invoke(
            main, ["-c", str(config), "activity", "tail", "--agent", "fw-bot"]
        )
        assert result.exit_code == 0, result.output
        assert "fw-bot (mcp-stdio:1-2)" in result.output
        assert "power_off" not in result.output

    def test_export_carries_agent(self, tmp_path):
        from labctl.cli import main

        config = self._setup(tmp_path)
        result = CliRunner().invoke(
            main, ["-c", str(config), "activity", "export", "--agent", "fw-bot"]
        )
        events = [json.loads(line) for line in result.output.splitlines()]
        assert [(e["action"], e["agent"]) for e in events] == [("power_on", "fw-bot")]


class TestClaimExpiryRecorded:
    """A claim that runs out is released by the system: record it."""

    def _expired(self, db, name):
        manager = ResourceManager(db)
        manager.create_sbc(name=name)
        manager.claim_sbc(
            sbc_name=name,
            agent_name=f"agent-{name}",
            session_id=f"cli-{name}",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        db.execute_modify(
            "UPDATE claims SET expires_at = '2000-01-01 00:00:00.000000'"
            " WHERE released_at IS NULL"
        )
        return manager

    def test_sweep_records_each_expired_claim_once(self, db):
        manager = self._expired(db, "pi")
        assert manager.expire_stale_claims(grace_seconds=0) == 1
        assert manager.expire_stale_claims(grace_seconds=0) == 0
        expired = [e for e in _events(db) if e["action"] == "claim_expired"]
        assert [(e["entity_name"], json.loads(e["details"])) for e in expired] == [
            ("pi", {"message": "Claim by agent-pi expired"})
        ]

    def test_claiming_sweeps_and_records_other_expired_claims(self, db):
        manager = self._expired(db, "pi")
        manager.create_sbc(name="pi2")
        manager.claim_sbc(
            sbc_name="pi2",
            agent_name="b",
            session_id="cli-b",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
            grace_seconds=0,
        )
        expired = [e for e in _events(db) if e["action"] == "claim_expired"]
        assert [e["entity_name"] for e in expired] == ["pi"]
