"""
Database management for lab controller.

Provides SQLite connection management, schema initialization, and migrations.

Concurrency notes
=================
labctl is multi-process by design (CLI, monitor daemon, MCP server, web app,
batch jobs all share one SQLite file). To keep concurrent readers and
writers from blocking each other:

* **WAL journal mode** is applied on first ``initialize()``. WAL is
  persisted in the database file header, so any process opening the
  file inherits it. With WAL, readers never block writers and vice
  versa — only writer-vs-writer is serialized, and labctl writes are
  all single-statement (sub-ms) or short transactions.
* **Per-connection busy_timeout** (default 10s, config-driven via
  ``database.timeout_seconds``) backstops the narrow windows where
  WAL still serializes — schema migrations and WAL checkpoints.
* **``synchronous=NORMAL``** trades a few-second worst-case write loss
  on power failure for substantially faster commits. Safe with WAL.
* **``initialize()`` is cached per-process** so short-lived CLI
  invocations don't repeatedly re-probe ``sqlite_master``.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger(__name__)

# Current schema version
SCHEMA_VERSION = 8

# Default SQLite busy-timeout when the Database is constructed without an
# explicit value (e.g. tests, direct callers). Matches Config.database
# default; raise only if a migration on a contended DB is timing out.
DEFAULT_TIMEOUT_SECONDS = 10.0

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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Cached power observation written by the monitor daemon every cycle.
    -- Read by `labctl status --fast` to avoid live network probes.
    last_power_state TEXT,         -- on | off | unknown
    last_power_at TIMESTAMP,       -- when the daemon last observed power
    -- Per-SBC power-cycle off→on delay. Used as both the default when
    -- --delay is omitted and the floor when --delay is passed (the CLI
    -- raises smaller values to this with a warning).
    power_cycle_delay_seconds REAL
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

-- Actuators (USB relays and similar single-bit hardware resources)
CREATE TABLE IF NOT EXISTS actuators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'relay',         -- relay | gpio | …
    driver TEXT NOT NULL,                       -- lcus1_serial | numato_acm | …
    device_path TEXT,                           -- /dev/lab/relay-rack1-a
    vid TEXT,                                   -- USB vendor id (hex string)
    pid TEXT,                                   -- USB product id (hex string)
    serial_no TEXT,                             -- USB serial number (when present)
    last_probe_at TIMESTAMP,                    -- last health-probe timestamp
    last_probe_result TEXT,                     -- ok | unreachable | …
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Channels (outlets) on an actuator
CREATE TABLE IF NOT EXISTS actuator_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actuator_id INTEGER NOT NULL,
    channel_index INTEGER NOT NULL,             -- 1-based
    label TEXT,
    default_state TEXT NOT NULL DEFAULT 'open'
        CHECK(default_state IN ('open','closed')),
    last_state TEXT
        CHECK(last_state IN ('open','closed') OR last_state IS NULL),
    last_changed_at TIMESTAMP,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (actuator_id) REFERENCES actuators(id) ON DELETE CASCADE,
    UNIQUE (actuator_id, channel_index)
);

-- Bindings: (SBC, purpose) → actuator_channel
CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,                      -- recovery_mode | boot_select | …
    actuator_channel_id INTEGER NOT NULL,
    shape_mode TEXT NOT NULL
        CHECK(shape_mode IN ('latch','momentary')),
    shape_active TEXT NOT NULL
        CHECK(shape_active IN ('closed','open')),
    momentary_pulse_ms INTEGER,
    sample_phase TEXT NOT NULL DEFAULT 'none'
        CHECK(sample_phase IN ('pre_power','post_power','none')),
    desired_state TEXT NOT NULL DEFAULT 'released'
        CHECK(desired_state IN ('asserted','released','following_power')),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
    FOREIGN KEY (actuator_channel_id)
        REFERENCES actuator_channels(id) ON DELETE CASCADE,
    UNIQUE (sbc_id, purpose),
    UNIQUE (actuator_channel_id)
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
CREATE INDEX IF NOT EXISTS idx_actuator_channels_actuator
    ON actuator_channels(actuator_id);
CREATE INDEX IF NOT EXISTS idx_bindings_sbc ON bindings(sbc_id);
CREATE INDEX IF NOT EXISTS idx_bindings_actuator_channel
    ON bindings(actuator_channel_id);
"""


class Database:
    """SQLite database manager for lab controller."""

    def __init__(
        self,
        db_path: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file.
            timeout_seconds: SQLite busy-wait used when another process
                holds an incompatible lock. Defaults to 10s; usually
                wired from ``Config.database.timeout_seconds``.
        """
        self.db_path = db_path
        self.timeout_seconds = timeout_seconds
        # Cache: only run the migration-probe block once per process per
        # Database instance. Short-lived CLI invocations still pay one
        # initialize(); long-lived processes (daemon, MCP, web) pay it
        # exactly once total. Reset by tests via _reset_initialized().
        self._initialized = False
        # RLock (not Lock) so a future migration or test helper that
        # happens to call back into initialize() while holding the lock
        # doesn't self-deadlock. Nothing in the current codebase does
        # this, but Lock made it a sharp edge.
        self._init_lock = threading.RLock()

    def initialize(self) -> None:
        """Initialize database schema if needed.

        Idempotent across calls within a single process: the first call
        runs the WAL/synchronous pragmas and the migration probe; every
        subsequent call is a near-no-op (only the cache flag is read
        under a lock). Multiple Python processes still each call this
        once on first DB access — which is fine because the pragmas are
        idempotent and the migration probe early-exits when current.
        """
        with self._init_lock:
            if self._initialized:
                return

            # Ensure directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            with self.connect() as conn:
                self._apply_persistent_pragmas(conn)
                # Check current schema version
                query = (
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='schema_version'"
                )
                cursor = conn.execute(query)
                if cursor.fetchone() is None:
                    # Fresh database - apply full schema.
                    # OR IGNORE on the version insert defends against the
                    # rare cross-process race where two fresh-DB
                    # initialize() calls interleave between the
                    # sqlite_master probe and the INSERT — both see no
                    # schema_version table, both run SCHEMA_SQL (idempotent
                    # via IF NOT EXISTS), then the second INSERT would
                    # otherwise fail with a PRIMARY KEY conflict.
                    _executescript_atomic(conn, SCHEMA_SQL)
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_version (version) "
                        "VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                else:
                    # Check for migrations
                    cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                    current_version = cursor.fetchone()[0] or 0
                    if current_version < SCHEMA_VERSION:
                        self._apply_migrations(conn, current_version)

            self._initialized = True

    def _apply_persistent_pragmas(self, conn: sqlite3.Connection) -> None:
        """Switch the database to WAL journal mode.

        ``journal_mode=WAL`` is persisted in the database file header,
        so any subsequent process that opens the file inherits it.
        Running it every ``initialize()`` is cheap and ensures a
        freshly-restored rollback-mode backup gets upgraded back to WAL
        on next start. ``synchronous`` is per-connection and set in
        ``connect()`` instead.
        """
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                # Read-only filesystem or otherwise unable to switch —
                # warn but keep going. Lock contention will fall back to
                # the connect()-level busy_timeout backstop.
                logger.warning(
                    "DB %s: requested journal_mode=WAL but got %r — "
                    "concurrent readers/writers may block each other.",
                    self.db_path,
                    mode,
                )
        except sqlite3.OperationalError as e:
            logger.warning(
                "DB %s: pragma setup failed: %s", self.db_path, e
            )

    def _reset_initialized(self) -> None:
        """Test-only: force the next initialize() to re-run."""
        with self._init_lock:
            self._initialized = False

    def _apply_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply database migrations."""
        if from_version < 2:
            # v2: Add serial_devices table, add alias/serial_device_id to serial_ports
            _executescript_atomic(
                conn,
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
            _executescript_atomic(
                conn,
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
            _executescript_atomic(
                conn,
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
            _executescript_atomic(
                conn,
                """
                CREATE TABLE IF NOT EXISTS audit_log (
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
                    claim_id INTEGER REFERENCES claims(id)
                );
                CREATE INDEX IF NOT EXISTS idx_audit_log_entity
                    ON audit_log(entity_type, entity_id);
                """,
            )
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
            _executescript_atomic(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_audit_log_logged_at
                    ON audit_log(logged_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_log_actor
                    ON audit_log(actor, logged_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_log_source
                    ON audit_log(source, logged_at DESC);
                """
            )

        if from_version < 6:
            # v6: cache the daemon's most recent power observation on each
            # SBC so `labctl status --fast` can render without live probes.
            for column_sql in (
                "ALTER TABLE sbcs ADD COLUMN last_power_state TEXT",
                "ALTER TABLE sbcs ADD COLUMN last_power_at TIMESTAMP",
            ):
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

        if from_version < 7:
            # v7: per-SBC power-cycle delay (used as both default and floor).
            try:
                conn.execute(
                    "ALTER TABLE sbcs ADD COLUMN power_cycle_delay_seconds REAL"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        if from_version < 8:
            # v8: actuators, actuator_channels, bindings — first-class
            # support for USB relays and per-target purpose bindings.
            _executescript_atomic(
                conn,
                """
                CREATE TABLE IF NOT EXISTS actuators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'relay',
                    driver TEXT NOT NULL,
                    device_path TEXT,
                    vid TEXT,
                    pid TEXT,
                    serial_no TEXT,
                    last_probe_at TIMESTAMP,
                    last_probe_result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS actuator_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actuator_id INTEGER NOT NULL,
                    channel_index INTEGER NOT NULL,
                    label TEXT,
                    default_state TEXT NOT NULL DEFAULT 'open'
                        CHECK(default_state IN ('open','closed')),
                    last_state TEXT
                        CHECK(last_state IN ('open','closed') OR last_state IS NULL),
                    last_changed_at TIMESTAMP,
                    cycle_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (actuator_id) REFERENCES actuators(id)
                        ON DELETE CASCADE,
                    UNIQUE (actuator_id, channel_index)
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sbc_id INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    actuator_channel_id INTEGER NOT NULL,
                    shape_mode TEXT NOT NULL
                        CHECK(shape_mode IN ('latch','momentary')),
                    shape_active TEXT NOT NULL
                        CHECK(shape_active IN ('closed','open')),
                    momentary_pulse_ms INTEGER,
                    sample_phase TEXT NOT NULL DEFAULT 'none'
                        CHECK(sample_phase IN ('pre_power','post_power','none')),
                    desired_state TEXT NOT NULL DEFAULT 'released'
                        CHECK(desired_state IN ('asserted','released','following_power')),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sbc_id) REFERENCES sbcs(id) ON DELETE CASCADE,
                    FOREIGN KEY (actuator_channel_id)
                        REFERENCES actuator_channels(id) ON DELETE CASCADE,
                    UNIQUE (sbc_id, purpose),
                    UNIQUE (actuator_channel_id)
                );
                CREATE INDEX IF NOT EXISTS idx_actuator_channels_actuator
                    ON actuator_channels(actuator_id);
                CREATE INDEX IF NOT EXISTS idx_bindings_sbc ON bindings(sbc_id);
                CREATE INDEX IF NOT EXISTS idx_bindings_actuator_channel
                    ON bindings(actuator_channel_id);
                """,
            )

        # OR IGNORE: tolerate a concurrent process that has already
        # bumped schema_version to the current value during a race.
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Get a database connection as a context manager.

        The ``timeout=`` arg makes ``sqlite3`` poll the busy lock for
        up to ``self.timeout_seconds`` before raising
        ``OperationalError``; the per-connection ``busy_timeout`` pragma
        is a belt-and-suspenders for sqlite3 stdlib versions where the
        ``timeout=`` plumbing has historically been buggy.

        Yields:
            SQLite connection with row factory set to sqlite3.Row.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        # Best-effort per-connection tuning. A caller without OS-level
        # write permission on the DB file opens the connection just
        # fine for reads, but SQLite refuses to execute write-side
        # pragmas on that connection ("attempt to write a readonly
        # database"). We log at debug and continue so unprivileged
        # readers (callers outside the ``labctl`` group, ad-hoc CLI
        # users, etc.) keep working after the WAL switch. Writable
        # connections still get the full tuning.
        for pragma in (
            # Mirrors the connect() timeout; belt-and-suspenders for
            # sqlite3 stdlib versions where `timeout=` plumbing has
            # historically been buggy.
            f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}",
            # synchronous=NORMAL is per-connection (not persisted in
            # the file like journal_mode), so it must be set on every
            # connect for writers. Pairs with WAL for substantially
            # faster commits at the cost of a few seconds of write
            # loss on a hard power failure — acceptable for a lab DB.
            "PRAGMA synchronous = NORMAL",
            # FK enforcement is per-connection. Irrelevant for a
            # read-only connection (no writes to enforce against).
            "PRAGMA foreign_keys = ON",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.OperationalError as e:
                logger.debug(
                    "connect(%s): pragma %r failed (read-only connection?): %s",
                    self.db_path,
                    pragma,
                    e,
                )
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


def get_database(
    db_path: Path, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Database:
    """
    Get an initialized database instance.

    Args:
        db_path: Path to database file.
        timeout_seconds: SQLite busy-wait. Usually wired from
            ``Config.database.timeout_seconds``; default 10s.

    Returns:
        Initialized Database instance.
    """
    db = Database(db_path, timeout_seconds=timeout_seconds)
    db.initialize()
    return db


def _executescript_atomic(conn: sqlite3.Connection, sql: str) -> None:
    """Run DDL in one explicit transaction to avoid slow statement commits."""
    conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
