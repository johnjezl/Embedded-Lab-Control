"""Unit tests for database module."""

import pytest
from pathlib import Path

from labctl.core.database import Database, get_database, SCHEMA_VERSION


class TestDatabase:
    """Tests for Database class."""

    def test_initialize_creates_tables(self, tmp_path):
        """Test that initialize creates all required tables."""
        db_path = tmp_path / "test.db"
        db = Database(db_path)
        db.initialize()

        # Check tables exist
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = [row["name"] for row in tables]

        assert "sbcs" in table_names
        assert "serial_ports" in table_names
        assert "network_addresses" in table_names
        assert "power_plugs" in table_names
        assert "status_log" in table_names
        assert "audit_log" in table_names
        assert "schema_version" in table_names

    def test_schema_version_recorded(self, tmp_path):
        """Test that schema version is recorded."""
        db_path = tmp_path / "test.db"
        db = Database(db_path)
        db.initialize()

        row = db.execute_one("SELECT MAX(version) as v FROM schema_version")
        assert row["v"] == SCHEMA_VERSION

    def test_execute_insert_returns_id(self, tmp_path):
        """Test execute_insert returns last row ID."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        sbc_id = db.execute_insert(
            "INSERT INTO sbcs (name) VALUES (?)", ("test-sbc",)
        )
        assert sbc_id == 1

        sbc_id2 = db.execute_insert(
            "INSERT INTO sbcs (name) VALUES (?)", ("test-sbc-2",)
        )
        assert sbc_id2 == 2

    def test_execute_one_returns_row(self, tmp_path):
        """Test execute_one returns single row."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        db.execute_insert("INSERT INTO sbcs (name, project) VALUES (?, ?)", ("sbc1", "proj1"))

        row = db.execute_one("SELECT * FROM sbcs WHERE name = ?", ("sbc1",))
        assert row is not None
        assert row["name"] == "sbc1"
        assert row["project"] == "proj1"

    def test_execute_one_returns_none_for_missing(self, tmp_path):
        """Test execute_one returns None for missing row."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        row = db.execute_one("SELECT * FROM sbcs WHERE name = ?", ("nonexistent",))
        assert row is None

    def test_execute_modify_returns_count(self, tmp_path):
        """Test execute_modify returns affected row count."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("sbc1",))
        db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("sbc2",))

        count = db.execute_modify("DELETE FROM sbcs WHERE name = ?", ("sbc1",))
        assert count == 1

        count = db.execute_modify("DELETE FROM sbcs")
        assert count == 1  # Only sbc2 left

    def test_foreign_keys_enabled(self, tmp_path):
        """Test foreign keys are enforced."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        # Try to insert serial port with non-existent SBC ID
        with pytest.raises(Exception):
            db.execute_insert(
                "INSERT INTO serial_ports (sbc_id, port_type, device_path) VALUES (?, ?, ?)",
                (999, "console", "/dev/test"),
            )

    def test_creates_parent_directory(self, tmp_path):
        """Test database creates parent directory if needed."""
        db_path = tmp_path / "subdir" / "nested" / "test.db"
        db = Database(db_path)
        db.initialize()

        assert db_path.exists()
