"""
Database management for lab controller.

Provides SQLite connection management, schema initialization, and migrations.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

# Current schema version
SCHEMA_VERSION = 5

# SQL statements for schema creation
SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- SBC (Single Board Computer) records
CREATE TABLE IF NOT EXISTS sbcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    project TEXT,
    description TEXT,
    ssh_user TEXT DEFAULT 'root',
    status TEXT DEFAULT 'unknown',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Registered USB-serial adapters
CREATE TABLE IF NOT EXISTS serial_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    usb_path TEXT UNIQUE NOT NULL,
    vendor TEXT,
    model TEXT,
    serial_number TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Serial port assignments
CREATE TABLE IF NOT EXISTS serial_ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER NOT NULL,
    port_type TEXT NOT NULL,  -- console, jtag, debug
    device_path TEXT NOT NULL,  -- /dev/lab/sbc1-console
    tcp_port INTEGER,
    baud_rate INTEGER DEFAULT 115200,
    alias TEXT,  -- human-friendly name for this assignment
    serial_device_id INTEGER,  -- FK to serial_devices
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
    FOREIGN KEY (serial_device_id) REFERENCES serial_devices(id) ON DELETE SET NULL,
    UNIQUE (sbc_id, port_type)
);

-- Network addresses
CREATE TABLE IF NOT EXISTS network_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER NOT NULL,
    address_type TEXT NOT NULL,  -- ethernet, wifi
    ip_address TEXT NOT NULL,
    mac_address TEXT,
    hostname TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
    UNIQUE (sbc_id, address_type)
);

-- Power plug assignments
CREATE TABLE IF NOT EXISTS power_plugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER UNIQUE NOT NULL,
    plug_type TEXT NOT NULL,  -- tasmota, kasa, shelly
    address TEXT NOT NULL,  -- IP or hostname
    plug_index INTEGER DEFAULT 1,  -- For multi-outlet strips
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
);

-- SDWire (SD card multiplexer) devices
CREATE TABLE IF NOT EXISTS sdwire_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    serial_number TEXT UNIQUE NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'sdwirec',  -- sdwire, sdwirec, sdwire3
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- SDWire assignments to SBCs
CREATE TABLE IF NOT EXISTS sdwire_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER UNIQUE NOT NULL,
    sdwire_device_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
    FOREIGN KEY (sdwire_device_id) REFERENCES sdwire_devices(id) ON DELETE CASCADE
);

-- Status history log
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
);

-- Audit log / activity stream — records every state-changing action
-- across CLI, MCP, web API, and the monitor daemon.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,  -- create, update, delete, power_on, power_off, etc.
    entity_type TEXT NOT NULL,  -- sbc, serial_port, power_plug, etc.
    entity_id INTEGER,
    entity_name TEXT,
    details TEXT,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor TEXT NOT NULL DEFAULT 'internal',     -- e.g. "cli:john", "mcp-stdio:12345-..."
    source TEXT NOT NULL DEFAULT 'internal',    -- cli | mcp | api | daemon | internal
    result TEXT NOT NULL DEFAULT 'ok',          -- ok | error | forbidden
    claim_id INTEGER REFERENCES claims(id)
);

-- Hardware claims (exclusive access coordination)
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_kind TEXT NOT NULL,  -- mcp-stdio, mcp-http, cli, web
    reason TEXT NOT NULL,
    context_json TEXT,
    acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duration_seconds INTEGER NOT NULL,
    last_activity TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,  -- materialized = last_activity + duration_seconds
    renewal_count INTEGER NOT NULL DEFAULT 0,
    released_at TIMESTAMP,
    release_reason TEXT,  -- released, expired, force-released, session-lost
    released_by TEXT,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
);

-- Release requests (polite nudges without forced eviction)
CREATE TABLE IF NOT EXISTS claim_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER NOT NULL DEFAULT 0,  -- SQLite boolean
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sbcs_project ON sbcs(project);
CREATE INDEX IF NOT EXISTS idx_sbcs_status ON sbcs(status);
CREATE INDEX IF NOT EXISTS idx_serial_devices_usb_path ON serial_devices(usb_path);
CREATE INDEX IF NOT EXISTS idx_serial_ports_sbc ON serial_ports(sbc_id);
CREATE INDEX IF NOT EXISTS idx_serial_ports_device ON serial_ports(device_path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_serial_ports_alias ON serial_ports(alias) WHERE alias IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sdwire_assignments_sbc ON sdwire_assignments(sbc_id);
CREATE INDEX IF NOT EXISTS idx_status_log_sbc ON status_log(sbc_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_logged_at ON audit_log(logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_source ON audit_log(source, logged_at DESC);

-- At most one active claim per SBC (partial unique index)
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active_sbc
    ON claims(sbc_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_session
    ON claims(session_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_agent
    ON claims(agent_name) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_expiry
    ON claims(expires_at) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_claim_requests_claim
    ON claim_requests(claim_id);
"""


class Database:
    """SQLite database manager for lab controller."""

    def __init__(self, db_path: Path):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path

    def initialize(self) -> None:
        """Initialize database schema if needed."""
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self.connect() as conn:
            # Check current schema version
            query = (
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_version'"
            )
            cursor = conn.execute(query)
            if cursor.fetchone() is None:
                # Fresh database - apply full schema
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            else:
                # Check for migrations
                cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                current_version = cursor.fetchone()[0] or 0
                if current_version < SCHEMA_VERSION:
                    self._apply_migrations(conn, current_version)

    def _apply_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply database migrations."""
        if from_version < 2:
            # v2: Add serial_devices table, add alias/serial_device_id to serial_ports
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS serial_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    usb_path TEXT UNIQUE NOT NULL,
                    vendor TEXT,
                    model TEXT,
                    serial_number TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_serial_devices_usb_path
                    ON serial_devices(usb_path);
            """
            )
            # ALTER TABLE cannot add FK constraints in SQLite, but the column works fine
            try:
                conn.execute("ALTER TABLE serial_ports ADD COLUMN alias TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "ALTER TABLE serial_ports ADD COLUMN serial_device_id INTEGER"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_serial_ports_alias "
                    "ON serial_ports(alias) WHERE alias IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass  # Index already exists

        if from_version < 3:
            # v3: Add SDWire device and assignment tables
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sdwire_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    serial_number TEXT UNIQUE NOT NULL,
                    device_type TEXT NOT NULL DEFAULT 'sdwirec',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sdwire_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sbc_id INTEGER UNIQUE NOT NULL,
                    sdwire_device_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                    FOREIGN KEY (sdwire_device_id) REFERENCES sdwire_devices(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sdwire_assignments_sbc
                    ON sdwire_assignments(sbc_id);
            """
            )

        if from_version < 4:
            # v4: Hardware claims (exclusive access coordination)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sbc_id INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    session_kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    context_json TEXT,
                    acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    duration_seconds INTEGER NOT NULL,
                    last_activity TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    renewal_count INTEGER NOT NULL DEFAULT 0,
                    released_at TIMESTAMP,
                    release_reason TEXT,
                    released_by TEXT,
                    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS claim_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL,
                    requested_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active_sbc
                    ON claims(sbc_id) WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_claims_session
                    ON claims(session_id) WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_claims_agent
                    ON claims(agent_name) WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_claims_expiry
                    ON claims(expires_at) WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_claim_requests_claim
                    ON claim_requests(claim_id);
            """
            )

        if from_version < 5:
            # v5: Activity stream — extend audit_log with actor/source/result/claim_id
            for column_sql in (
                "ALTER TABLE audit_log ADD COLUMN actor TEXT NOT NULL DEFAULT 'internal'",
                "ALTER TABLE audit_log ADD COLUMN source TEXT NOT NULL DEFAULT 'internal'",
                "ALTER TABLE audit_log ADD COLUMN result TEXT NOT NULL DEFAULT 'ok'",
                "ALTER TABLE audit_log ADD COLUMN claim_id INTEGER REFERENCES claims(id)",
            ):
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_log_logged_at
                    ON audit_log(logged_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_log_actor
                    ON audit_log(actor, logged_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_log_source
                    ON audit_log(source, logged_at DESC);
                """
            )

        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Get a database connection as a context manager.

        Yields:
            SQLite connection with row factory set to sqlite3.Row
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enable foreign keys
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(
        self,
        sql: str,
        params: tuple = (),
    ) -> list[sqlite3.Row]:
        """
        Execute SQL and return all results.

        Args:
            sql: SQL statement
            params: Query parameters

        Returns:
            List of result rows
        """
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()

    def execute_one(
        self,
        sql: str,
        params: tuple = (),
    ) -> Optional[sqlite3.Row]:
        """
        Execute SQL and return single result.

        Args:
            sql: SQL statement
            params: Query parameters

        Returns:
            Single result row or None
        """
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()

    def execute_insert(
        self,
        sql: str,
        params: tuple = (),
    ) -> int:
        """
        Execute INSERT and return last row ID.

        Args:
            sql: INSERT statement
            params: Query parameters

        Returns:
            ID of inserted row
        """
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid

    def execute_modify(
        self,
        sql: str,
        params: tuple = (),
    ) -> int:
        """
        Execute UPDATE/DELETE and return affected row count.

        Args:
            sql: UPDATE or DELETE statement
            params: Query parameters

        Returns:
            Number of affected rows
        """
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount


def get_database(db_path: Path) -> Database:
    """
    Get an initialized database instance.

    Args:
        db_path: Path to database file

    Returns:
        Initialized Database instance
    """
    db = Database(db_path)
    db.initialize()
    return db
