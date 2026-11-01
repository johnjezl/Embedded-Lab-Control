"""Timestamps are UTC in storage and output, local (with zone) for people,
and claims behave across a DST change (#63)."""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from labctl.core import audit, timeutil
from labctl.core.database import Database, get_database
from labctl.core.manager import ResourceManager

UTC = timezone.utc


@pytest.fixture
def pacific():
    """Run with the process in US Pacific time (DST ends 2026-11-01)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def manager(tmp_path):
    return ResourceManager(get_database(tmp_path / "labctl.db"))


class TestTimeutil:
    def test_round_trip_and_output(self):
        dt = datetime(2026, 11, 1, 8, 52, 47, 123456, tzinfo=UTC)
        assert timeutil.to_db(dt) == "2026-11-01 08:52:47.123456"
        assert timeutil.from_db("2026-11-01 08:52:47.123456") == dt
        assert timeutil.from_db("2026-11-01T01:52:47.123456-07:00") == dt
        assert timeutil.iso(dt) == "2026-11-01T08:52:47.123456Z"
        assert timeutil.from_db("not a time") is None

    @pytest.mark.parametrize(
        "text",
        [
            "2026-11-01 12:00:00.5",
            "2026-11-01T12:00:00.5Z",
            "2026-11-01T12:00:00.5000+0000",
            "2026-11-01T04:00:00.500000-08:00",
            "2026-11-01 12:00:00.500000000",
        ],
    )
    def test_shapes_python_310_rejects(self, text):
        assert timeutil.from_db(text) == datetime(
            2026, 11, 1, 12, 0, 0, 500000, tzinfo=UTC
        )

    def test_machine_output_always_has_microseconds(self):
        assert timeutil.db_iso("2026-11-01 12:00:00") == "2026-11-01T12:00:00.000000Z"

    def test_local_display_names_the_zone(self, pacific):
        before = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)  # 01:30 PDT
        after = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST
        assert timeutil.local(before) == "2026-11-01 01:30:00 PDT"
        assert timeutil.local(after) == "2026-11-01 01:30:00 PST"


class TestClaimsAcrossTheFallBack:
    """The agent's case: claimed at 01:52 PDT, renewed at 01:00 PST."""

    def test_renewal_after_the_fall_back(self, manager, pacific, monkeypatch):
        clock = [datetime(2026, 11, 1, 8, 52, 47, tzinfo=UTC)]  # 01:52 PDT
        monkeypatch.setattr("labctl.core.timeutil.now", lambda: clock[0])
        manager.create_sbc(name="pi")
        first = manager.claim_sbc(
            sbc_name="pi",
            agent_name="a",
            session_id="s",
            session_kind="cli",
            duration_seconds=3600,
            reason="r",
        )
        clock[0] = datetime(2026, 11, 1, 9, 0, 24, tzinfo=UTC)  # 01:00 PST
        renewed = manager.renew_claim("pi", "s", duration_seconds=5400)
        assert renewed.last_activity > renewed.acquired_at
        assert renewed.expires_at > first.expires_at
        assert renewed.expires_at == clock[0] + timedelta(seconds=5400)
        assert renewed.is_active
        assert renewed.to_dict()["expires_at"] == "2026-11-01T10:30:24.000000Z"

    def test_expiry_is_on_time(self, manager, pacific, monkeypatch):
        clock = [datetime(2026, 11, 1, 8, 50, tzinfo=UTC)]  # 01:50 PDT
        monkeypatch.setattr("labctl.core.timeutil.now", lambda: clock[0])
        manager.create_sbc(name="pi")
        manager.claim_sbc(
            sbc_name="pi",
            agent_name="a",
            session_id="s",
            session_kind="cli",
            duration_seconds=3600,
            reason="r",
        )
        clock[0] += timedelta(minutes=61)  # 01:51 PST: an hour and a minute on
        assert manager.get_active_claim("pi") is None
        assert manager.expire_stale_claims(grace_seconds=0) == 1


class TestMigration:
    @pytest.fixture(autouse=True)
    def host_zone(self, monkeypatch):
        """The host's zone is Pacific, whatever the test process's TZ."""
        from zoneinfo import ZoneInfo

        monkeypatch.setattr(
            "labctl.core.database._system_zone",
            lambda: ZoneInfo("America/Los_Angeles"),
        )

    def test_converted_in_the_hosts_zone_not_the_processes(self, tmp_path):
        """A process with TZ=UTC opening the database first still converts
        from the host's zone."""
        old = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            path = tmp_path / "labctl.db"
            self._v13_db(
                path, [("2026-10-31 17:00:00.000000", "2026-10-31 18:00:00.000000")]
            )
            db = Database(path)
            db.initialize()
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        claim = db.execute_one("SELECT acquired_at FROM claims")
        assert claim["acquired_at"] == "2026-11-01 00:00:00.000000"

    def _v13_db(self, path, rows):
        Database(path).initialize()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO sbcs (name) VALUES ('pi')")
        for acquired, expires in rows:
            conn.execute(
                "INSERT INTO claims (sbc_id, agent_name, session_id, session_kind,"
                " reason, acquired_at, duration_seconds, last_activity, expires_at)"
                " VALUES (1, 'a', 's', 'cli', 'r', ?, 60, ?, ?)",
                (acquired, acquired, expires),
            )
        conn.execute(
            "INSERT INTO audit_log (action, entity_type, logged_at)"
            " VALUES ('claim', 'sbc', '2026-10-31 17:00:00.250')"
        )
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (13)")
        conn.commit()
        conn.close()

    def test_local_times_become_utc(self, tmp_path, pacific):
        path = tmp_path / "labctl.db"
        self._v13_db(
            path,
            [("2026-10-31 17:00:00.000000", "2026-10-31 18:00:00.000000")],
        )
        db = Database(path)
        db.initialize()
        claim = db.execute_one("SELECT acquired_at, expires_at FROM claims")
        assert claim["acquired_at"] == "2026-11-01 00:00:00.000000"  # PDT +7
        assert claim["expires_at"] == "2026-11-01 01:00:00.000000"
        event = db.execute_one("SELECT logged_at FROM audit_log")
        assert event["logged_at"] == "2026-11-01 00:00:00.250"
        version = db.execute_one("SELECT MAX(version) AS v FROM schema_version")
        assert version["v"] == 14

    def test_repeated_hour_read_as_daylight_time(self, tmp_path, pacific):
        path = tmp_path / "labctl.db"
        self._v13_db(
            path, [("2026-11-01 01:30:00.000000", "2026-11-01 02:30:00.000000")]
        )
        db = Database(path)
        db.initialize()
        claim = db.execute_one("SELECT acquired_at FROM claims")
        assert claim["acquired_at"] == "2026-11-01 08:30:00.000000"  # 01:30 PDT

    def test_sqlite_stamped_rows_are_left_alone(self, tmp_path, pacific):
        """A value without a fraction came from DEFAULT CURRENT_TIMESTAMP,
        which was UTC already."""
        path = tmp_path / "labctl.db"
        self._v13_db(
            path, [("2026-10-31 17:00:00.000000", "2026-10-31 18:00:00.000000")]
        )
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO audit_log (action, entity_type, logged_at)"
            " VALUES ('old', 'sbc', '2026-04-01 22:14:05')"
        )
        conn.commit()
        conn.close()
        db = Database(path)
        db.initialize()
        row = db.execute_one("SELECT logged_at FROM audit_log WHERE action = 'old'")
        assert row["logged_at"] == "2026-04-01 22:14:05"

    def test_older_labctl_refuses_a_newer_database(self, tmp_path, monkeypatch):
        from labctl.cli import main

        path = tmp_path / "labctl.db"
        Database(path).initialize()
        monkeypatch.setattr("labctl.core.database.SCHEMA_VERSION", 13)
        with pytest.raises(RuntimeError, match="newer than this labctl"):
            Database(path).initialize()
        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {path}\n")
        result = CliRunner().invoke(main, ["-c", str(config), "list"])
        assert result.exit_code == 1
        assert "Error: database" in result.output
        assert "Traceback" not in result.output

    def test_a_second_initialize_doesnt_shift_again(self, tmp_path, pacific):
        path = tmp_path / "labctl.db"
        self._v13_db(
            path, [("2026-10-31 17:00:00.000000", "2026-10-31 18:00:00.000000")]
        )
        Database(path).initialize()
        Database(path).initialize()
        db = Database(path)
        db.initialize()
        claim = db.execute_one("SELECT acquired_at FROM claims")
        assert claim["acquired_at"] == "2026-11-01 00:00:00.000000"

    def test_racing_migration_converts_once(self, tmp_path, pacific):
        """Two processes see v13; the second finds v14 under the lock."""
        from labctl.core.database import Database as D

        path = tmp_path / "labctl.db"
        self._v13_db(
            path, [("2026-10-31 17:00:00.000000", "2026-10-31 18:00:00.000000")]
        )
        first, second = D(path), D(path)
        with first.connect() as c1:
            first._apply_migrations(c1, 13)
        with second.connect() as c2:
            second._apply_migrations(c2, 13)  # stale version read
        claim = first.execute_one("SELECT acquired_at FROM claims")
        assert claim["acquired_at"] == "2026-11-01 00:00:00.000000"


def test_released_claims_pruned_against_utc(manager, pacific):
    """released_at was always UTC; the cutoff now is too."""
    manager.create_sbc(name="pi")
    manager.claim_sbc(
        sbc_name="pi",
        agent_name="a",
        session_id="s",
        session_kind="cli",
        duration_seconds=60,
        reason="r",
    )
    manager.release_claim("pi", "s")
    # Released 2 hours ago (UTC): not past a 1-day retention.
    manager.db.execute_modify(
        "UPDATE claims SET released_at = datetime('now', '-2 hours')"
    )
    assert manager.prune_released_claims(older_than_days=1) == 0
    manager.db.execute_modify(
        "UPDATE claims SET released_at = datetime('now', '-25 hours')"
    )
    assert manager.prune_released_claims(older_than_days=1) == 1


class TestActivity:
    def test_stored_utc_exported_with_z_shown_local(self, tmp_path, pacific):
        from labctl.cli import main

        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        db = get_database(tmp_path / "labctl.db")
        fixed = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST
        with patch("labctl.core.timeutil.now", return_value=fixed):
            audit.emit(db, action="power_on", entity_type="sbc", entity_name="pi")
        row = db.execute_one("SELECT logged_at FROM audit_log")
        assert row["logged_at"] == "2026-11-01 09:30:00.000"
        (event,) = audit.query_events(db)
        assert event["logged_at"] == "2026-11-01T09:30:00.000000Z"
        out = CliRunner().invoke(main, ["-c", str(config), "activity", "tail"]).output
        assert "2026-11-01 01:30:00 PST" in out

    def test_since_with_an_offset(self, tmp_path):
        db = get_database(tmp_path / "labctl.db")
        with patch(
            "labctl.core.timeutil.now",
            return_value=datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
        ):
            audit.emit(db, action="x", entity_type="sbc")
        assert audit.query_events(db, since="2026-11-01T01:00:00-08:00")
        assert not audit.query_events(db, since="2026-11-01T02:00:00-08:00")
        with pytest.raises(ValueError, match="not a timestamp"):
            audit.query_events(db, since="yesterday-ish")
