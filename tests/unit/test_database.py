"""Unit tests for database module."""

import sqlite3
import threading

import pytest

from labctl.core.database import (
    DEFAULT_TIMEOUT_SECONDS,
    SCHEMA_VERSION,
    Database,
    get_database,
)


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
        assert "claims" in table_names
        assert "claim_requests" in table_names

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
        """
        )
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
        """
        )
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
        conn.executescript(
            """
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
        """
        )
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

    def test_schema_v4_creates_claim_tables(self, tmp_path):
        """Test that fresh init creates claims and claim_requests tables with expected columns."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        cols = db.execute("PRAGMA table_info(claims)")
        col_names = {c["name"] for c in cols}
        required = {
            "sbc_id",
            "agent_name",
            "session_id",
            "session_kind",
            "reason",
            "context_json",
            "acquired_at",
            "duration_seconds",
            "last_activity",
            "expires_at",
            "renewal_count",
            "released_at",
            "release_reason",
            "released_by",
        }
        assert required.issubset(col_names)

        cols = db.execute("PRAGMA table_info(claim_requests)")
        col_names = {c["name"] for c in cols}
        assert {
            "claim_id",
            "requested_by",
            "reason",
            "requested_at",
            "acknowledged",
        }.issubset(col_names)

    def test_schema_v4_partial_unique_index_allows_multiple_released(self, tmp_path):
        """Released claims can repeat per SBC; only active ones are constrained."""
        db_path = tmp_path / "test.db"
        db = get_database(db_path)

        db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("sbc1",))
        # Three released claims on the same SBC — all allowed
        for _ in range(3):
            db.execute_insert(
                """
                INSERT INTO claims
                  (sbc_id, agent_name, session_id, session_kind, reason,
                   duration_seconds, expires_at, released_at, release_reason)
                VALUES (1, 'a', 's', 'cli', 'r', 60, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, 'released')
                """,
            )

        # One active claim is fine
        db.execute_insert(
            """
            INSERT INTO claims
              (sbc_id, agent_name, session_id, session_kind, reason,
               duration_seconds, expires_at)
            VALUES (1, 'a', 's', 'cli', 'r', 60, CURRENT_TIMESTAMP)
            """,
        )

        # A second active claim on the same SBC must fail
        with pytest.raises(Exception):
            db.execute_insert(
                """
                INSERT INTO claims
                  (sbc_id, agent_name, session_id, session_kind, reason,
                   duration_seconds, expires_at)
                VALUES (1, 'b', 't', 'cli', 'r', 60, CURRENT_TIMESTAMP)
                """,
            )

    def test_migration_v3_to_v4(self, tmp_path):
        """v3 -> v4 adds claim tables without disturbing existing data."""
        import sqlite3

        db_path = tmp_path / "test_v3_to_v4.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
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
            INSERT INTO schema_version (version) VALUES (3);
            INSERT INTO sbcs (name) VALUES ('pre-existing');
        """
        )
        conn.commit()
        conn.close()

        db = Database(db_path)
        db.initialize()

        # Claims tables now exist
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='claims'"
        )
        assert len(rows) == 1
        rows = db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='claim_requests'"
        )
        assert len(rows) == 1

        # Partial unique index is in place
        rows = db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_claims_active_sbc'"
        )
        assert len(rows) == 1

        # Existing data preserved
        row = db.execute_one("SELECT name FROM sbcs WHERE name = 'pre-existing'")
        assert row is not None

        row = db.execute_one("SELECT MAX(version) as v FROM schema_version")
        assert row["v"] == SCHEMA_VERSION


class TestConcurrency:
    """Concurrency settings — WAL mode, busy_timeout, initialize caching.

    These are the fix for the transient ``database is locked`` errors
    seen under multi-process load. See database.py module docstring.
    """

    def test_wal_mode_applied_on_init(self, tmp_path):
        """initialize() switches the journal mode to WAL."""
        db_path = tmp_path / "wal.db"
        db = Database(db_path)
        db.initialize()
        mode = db.execute_one("PRAGMA journal_mode")
        assert mode[0].lower() == "wal"

    def test_synchronous_normal_applied_on_init(self, tmp_path):
        """initialize() sets synchronous=NORMAL (the WAL-safe pairing)."""
        db_path = tmp_path / "sync.db"
        db = Database(db_path)
        db.initialize()
        # PRAGMA synchronous returns 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        result = db.execute_one("PRAGMA synchronous")
        assert result[0] == 1, (
            f"expected synchronous=NORMAL (1), got {result[0]}"
        )

    def test_busy_timeout_applied_per_connection(self, tmp_path):
        """Every connect() sets busy_timeout to match timeout_seconds."""
        db_path = tmp_path / "bt.db"
        db = Database(db_path, timeout_seconds=7.5)
        db.initialize()
        with db.connect() as conn:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            assert row[0] == 7500  # milliseconds

    def test_default_timeout_is_ten_seconds(self):
        """Sanity-check the module-level default the config inherits."""
        assert DEFAULT_TIMEOUT_SECONDS == 10.0

    def test_initialize_is_cached_per_instance(self, tmp_path):
        """Repeated initialize() calls on one instance do no further DB work.

        The second call must not re-run the sqlite_master probe, since
        that's the lock-acquisition surface we're trying to shrink.
        """
        db_path = tmp_path / "cache.db"
        db = Database(db_path)
        db.initialize()
        assert db._initialized is True

        # Patch connect to detect any further DB access during a second
        # initialize(). If the cache is honored, connect() is not called.
        called = []
        original_connect = db.connect

        def watch():
            called.append(1)
            return original_connect()

        db.connect = watch  # type: ignore[assignment]
        db.initialize()
        assert called == [], (
            "second initialize() opened a connection — cache not honored"
        )

    def test_reset_initialized_re_runs(self, tmp_path):
        """_reset_initialized lets tests force a fresh initialize."""
        db_path = tmp_path / "reset.db"
        db = Database(db_path)
        db.initialize()
        db._reset_initialized()
        assert db._initialized is False
        db.initialize()  # must not raise
        assert db._initialized is True

    def test_writer_can_commit_during_concurrent_read(self, tmp_path):
        """WAL-specific property: a writer can COMMIT while another
        connection holds an open read transaction.

        In rollback-journal mode the writer's COMMIT must upgrade from
        RESERVED to EXCLUSIVE, which blocks until every SHARED lock
        drops — so a concurrent reader holding a transaction would
        force the writer to wait for the reader's busy_timeout. With
        the timeouts set to 100ms here, a regression to rollback mode
        would surface as ``OperationalError: database is locked`` on
        the writer's commit; with WAL it just succeeds.
        """
        db_path = tmp_path / "wal_commit.db"
        db = get_database(db_path)
        db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("baseline",))

        reader_holding = threading.Event()
        reader_release = threading.Event()
        writer_done = threading.Event()
        writer_error: list = []

        def reader():
            # Open a deferred transaction and hold a read lock on the file.
            conn = sqlite3.connect(str(db_path), timeout=0.1)
            try:
                conn.execute("BEGIN")
                conn.execute(
                    "SELECT name FROM sbcs WHERE name=?",
                    ("baseline",),
                ).fetchone()
                reader_holding.set()
                reader_release.wait(timeout=10.0)
                conn.rollback()
            finally:
                conn.close()

        def writer():
            reader_holding.wait(timeout=5.0)
            conn = sqlite3.connect(str(db_path), timeout=0.1)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO sbcs (name) VALUES (?)", ("writer-2",)
                )
                # The COMMIT is the WAL-discriminating step: in
                # rollback mode this raises within 100ms (busy_timeout
                # set on the connect). With WAL it returns immediately.
                conn.commit()
            except sqlite3.OperationalError as e:
                writer_error.append(str(e))
            finally:
                conn.close()
            writer_done.set()

        rt = threading.Thread(target=reader)
        wt = threading.Thread(target=writer)
        rt.start()
        wt.start()
        try:
            assert writer_done.wait(timeout=5.0), (
                "writer never finished — coordination event missed"
            )
            assert writer_error == [], (
                f"writer.commit() raised {writer_error!r} — WAL probably "
                "not in effect; rollback-mode commit blocked on the "
                "reader's SHARED lock"
            )
        finally:
            reader_release.set()
            rt.join(timeout=5.0)
            wt.join(timeout=5.0)

        # Sanity: the writer's insert actually committed.
        assert db.execute_one(
            "SELECT name FROM sbcs WHERE name=?", ("writer-2",)
        ) is not None

    def test_get_database_threads_timeout(self, tmp_path):
        """get_database forwards timeout_seconds to the Database it builds."""
        db_path = tmp_path / "thread.db"
        db = get_database(db_path, timeout_seconds=3.0)
        assert db.timeout_seconds == 3.0

    def test_schema_version_insert_is_idempotent(self, tmp_path):
        """A second fresh-DB bootstrap against the same path must not
        raise a UNIQUE-constraint error.

        This simulates the cross-process race where two fresh
        ``initialize()`` calls interleave between the sqlite_master
        probe and the schema_version INSERT. ``INSERT OR IGNORE`` is
        what keeps this clean.
        """
        db_path = tmp_path / "race.db"
        db1 = Database(db_path)
        db1.initialize()

        # A second Database instance with the same path, forced to
        # re-run its fresh-DB code path even though the file now exists.
        db2 = Database(db_path)
        # Drop the schema_version row but leave the table, simulating
        # a half-finished bootstrap by a sibling process.
        with db2.connect() as conn:
            conn.execute("DELETE FROM schema_version")
        # Manually invoke initialize's bootstrap-INSERT path. Without
        # OR IGNORE this would still pass (table was dropped to empty),
        # so also re-run with the row present to assert no constraint
        # violation.
        db2._reset_initialized()
        db2.initialize()
        with db2.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=?",
                (SCHEMA_VERSION,),
            ).fetchone()
        assert row[0] == 1, "schema_version row should be present exactly once"

    def test_connect_tolerates_readonly_connection(self, tmp_path, monkeypatch):
        """Regression: after the WAL switch, a connection against a
        read-only DB used to fail at ``PRAGMA synchronous=NORMAL``
        with ``attempt to write a readonly database`` — breaking any
        unprivileged caller (e.g. a user outside the ``labctl`` group
        running ``labctl serial send``). Pragmas are now best-effort
        so reads still work.

        We simulate the production scenario by monkey-patching
        ``sqlite3.connect`` to open the file via the ``mode=ro`` URI.
        SQLite's auto-downgrade-on-EACCES path produces the same
        connection-marked-readonly state; URI is just a deterministic
        way to reach it from a test.
        """
        # Create + populate the DB while the file is writable.
        db_path = tmp_path / "ro.db"
        db = get_database(db_path)
        db.execute_insert("INSERT INTO sbcs (name) VALUES (?)", ("ro-test",))

        real_connect = sqlite3.connect

        def ro_connect(target, *args, **kwargs):
            # Force read-only via URI regardless of the path argument.
            return real_connect(
                f"file:{target}?mode=ro", *args, uri=True, **kwargs
            )

        monkeypatch.setattr(sqlite3, "connect", ro_connect)

        ro = Database(db_path)
        # connect() must not raise; SELECT must succeed despite the
        # pragmas failing to apply.
        row = ro.execute_one(
            "SELECT name FROM sbcs WHERE name=?", ("ro-test",)
        )
        assert row is not None
        assert row["name"] == "ro-test"

    def test_init_lock_is_reentrant(self):
        """_init_lock is an RLock so a callback into initialize() while
        the lock is held wouldn't self-deadlock. Verified by acquiring
        the lock twice from the same thread without timing out."""
        db = Database(__file__)  # path irrelevant; we only touch the lock
        # If this were a plain Lock, the second acquire would block.
        assert db._init_lock.acquire(timeout=0.1)
        try:
            assert db._init_lock.acquire(timeout=0.1)
            db._init_lock.release()
        finally:
            db._init_lock.release()
