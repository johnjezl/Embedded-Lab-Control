"""Unit tests for database module."""

import pytest

from labctl.core.database import SCHEMA_VERSION, Database, get_database


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

        sbc_id = db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("test-sbc",))
        assert sbc_id == 1

        sbc_id2 = db.execute_insert(
            "INSERT INTO sbcs (name) VALUES (?)", ("test-sbc-2",)
        )
        assert sbc_id2 == 2

    def test_execute_one_returns_row(self, tmp_path):
        """Test execute_one returns single row."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        db.execute_insert(
            "INSERT INTO sbcs (name, project) VALUES (?, ?)", ("sbc1", "proj1")
        )

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
            sql = (
                "INSERT INTO serial_ports (sbc_id, port_type, device_path) "
                "VALUES (?, ?, ?)"
            )
            db.execute_insert(sql, (999, "console", "/dev/test"))

    def test_creates_parent_directory(self, tmp_path):
        """Test database creates parent directory if needed."""
        db_path = tmp_path / "subdir" / "nested" / "test.db"
        db = Database(db_path)
        db.initialize()

        assert db_path.exists()

    def test_schema_v2_creates_serial_devices_table(self, tmp_path):
        """Test that schema v2 creates the serial_devices table with correct columns."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        # Verify serial_devices table exists
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='serial_devices'"
        )
        assert len(tables) == 1

        # Verify columns on serial_devices
        cols = db.execute("PRAGMA table_info(serial_devices)")
        col_names = [row["name"] for row in cols]
        assert "id" in col_names
        assert "name" in col_names
        assert "usb_path" in col_names
        assert "vendor" in col_names
        assert "model" in col_names
        assert "serial_number" in col_names
        assert "created_at" in col_names

    def test_schema_v2_serial_ports_has_alias_and_device_id(self, tmp_path):
        """Test that serial_ports table has alias and serial_device_id columns."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        cols = db.execute("PRAGMA table_info(serial_ports)")
        col_names = [row["name"] for row in cols]
        assert "alias" in col_names
        assert "serial_device_id" in col_names

    def test_migration_v1_to_v2(self, tmp_path):
        """Test migration from schema v1 to v2 adds serial_devices and new columns."""
        import sqlite3

        db_path = tmp_path / "test_migrate.db"

        # Manually create a v1-like database
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
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

            CREATE TABLE serial_ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                port_type TEXT NOT NULL,
                device_path TEXT NOT NULL,
                tcp_port INTEGER,
                baud_rate INTEGER DEFAULT 115200,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, port_type)
            );

            CREATE TABLE network_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                address_type TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                mac_address TEXT,
                hostname TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, address_type)
            );

            CREATE TABLE power_plugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER UNIQUE NOT NULL,
                plug_type TEXT NOT NULL,
                address TEXT NOT NULL,
                plug_index INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
            );

            CREATE TABLE status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
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

            INSERT INTO schema_version (version) VALUES (1);
        """)
        conn.commit()
        conn.close()

        # Now open with Database which should trigger migration
        db = Database(db_path)
        db.initialize()

        # Verify serial_devices table was created
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='serial_devices'"
        )
        assert len(tables) == 1

        # Verify alias and serial_device_id columns were added to serial_ports
        cols = db.execute("PRAGMA table_info(serial_ports)")
        col_names = [row["name"] for row in cols]
        assert "alias" in col_names
        assert "serial_device_id" in col_names

        # Verify schema version was bumped to latest
        from labctl.core.database import SCHEMA_VERSION

        row = db.execute_one("SELECT MAX(version) as v FROM schema_version")
        assert row["v"] == SCHEMA_VERSION

    def test_migration_v1_to_v2_preserves_existing_data(self, tmp_path):
        """Test that v1->v2 migration does not lose existing serial_ports data."""
        import sqlite3

        db_path = tmp_path / "test_preserve.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
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

            CREATE TABLE serial_ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                port_type TEXT NOT NULL,
                device_path TEXT NOT NULL,
                tcp_port INTEGER,
                baud_rate INTEGER DEFAULT 115200,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, port_type)
            );

            CREATE TABLE network_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                address_type TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                mac_address TEXT,
                hostname TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, address_type)
            );

            CREATE TABLE power_plugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER UNIQUE NOT NULL,
                plug_type TEXT NOT NULL,
                address TEXT NOT NULL,
                plug_index INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
            );

            CREATE TABLE status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
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

            INSERT INTO schema_version (version) VALUES (1);
            INSERT INTO sbcs (name) VALUES ('existing-sbc');
            INSERT INTO serial_ports (sbc_id, port_type, device_path, tcp_port, baud_rate)
                VALUES (1, 'console', '/dev/ttyUSB0', 4000, 115200);
        """)
        conn.commit()
        conn.close()

        # Run migration
        db = Database(db_path)
        db.initialize()

        # Existing data should still be present
        row = db.execute_one("SELECT * FROM serial_ports WHERE sbc_id = 1")
        assert row is not None
        assert row["device_path"] == "/dev/ttyUSB0"
        assert row["tcp_port"] == 4000
        # New columns should be NULL for existing rows
        assert row["alias"] is None
        assert row["serial_device_id"] is None

    def test_schema_v3_creates_sdwire_tables(self, tmp_path):
        """Test that schema v3 creates sdwire_devices and sdwire_assignments tables."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        # Check sdwire_devices table
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sdwire_devices'"
        )
        assert len(rows) == 1

        # Check columns
        cols = db.execute("PRAGMA table_info(sdwire_devices)")
        col_names = [c["name"] for c in cols]
        assert "name" in col_names
        assert "serial_number" in col_names
        assert "device_type" in col_names

        # Check sdwire_assignments table
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sdwire_assignments'"
        )
        assert len(rows) == 1

        cols = db.execute("PRAGMA table_info(sdwire_assignments)")
        col_names = [c["name"] for c in cols]
        assert "sbc_id" in col_names
        assert "sdwire_device_id" in col_names

    def test_migration_v2_to_v3(self, tmp_path):
        """Test migration from v2 to v3 creates SDWire tables."""
        import sqlite3

        db_path = tmp_path / "test_v2_to_v3.db"

        # Create a v2 database (has serial_devices but no sdwire tables)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE sbcs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                project TEXT, description TEXT,
                ssh_user TEXT DEFAULT 'root',
                status TEXT DEFAULT 'unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE serial_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                usb_path TEXT UNIQUE NOT NULL,
                vendor TEXT, model TEXT, serial_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE serial_ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                port_type TEXT NOT NULL,
                device_path TEXT NOT NULL,
                tcp_port INTEGER,
                baud_rate INTEGER DEFAULT 115200,
                alias TEXT,
                serial_device_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, port_type)
            );
            CREATE TABLE network_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                address_type TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                mac_address TEXT, hostname TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                UNIQUE (sbc_id, address_type)
            );
            CREATE TABLE power_plugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER UNIQUE NOT NULL,
                plug_type TEXT NOT NULL,
                address TEXT NOT NULL,
                plug_index INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
            );
            CREATE TABLE status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sbc_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER, entity_name TEXT, details TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO schema_version (version) VALUES (2);
        """)
        conn.commit()
        conn.close()

        # Run migration
        db = Database(db_path)
        db.initialize()

        # SDWire tables should now exist
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sdwire_devices'"
        )
        assert len(rows) == 1

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sdwire_assignments'"
        )
        assert len(rows) == 1

        # Schema version should be current
        from labctl.core.database import SCHEMA_VERSION
        row = db.execute_one("SELECT MAX(version) as v FROM schema_version")
        assert row["v"] == SCHEMA_VERSION
