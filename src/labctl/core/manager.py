"""
Resource manager for lab controller.

Provides CRUD operations for SBCs, serial ports, network addresses, and power plugs.
"""

from pathlib import Path
from typing import Optional

from labctl.core.database import Database, get_database
from labctl.core.models import (
    SBC,
    AddressType,
    NetworkAddress,
    PlugType,
    PortType,
    PowerPlug,
    SerialPort,
    Status,
)


class ResourceManager:
    """Manager for lab resources (SBCs, ports, etc.)."""

    def __init__(self, db: Database):
        """
        Initialize resource manager.

        Args:
            db: Initialized database instance
        """
        self.db = db

    # --- SBC Operations ---

    def create_sbc(
        self,
        name: str,
        project: Optional[str] = None,
        description: Optional[str] = None,
        ssh_user: str = "root",
    ) -> SBC:
        """
        Create a new SBC record.

        Args:
            name: Unique name for the SBC
            project: Optional project name
            description: Optional description
            ssh_user: SSH username (default: root)

        Returns:
            Created SBC instance

        Raises:
            ValueError: If name already exists
        """
        # Check for duplicate name
        existing = self.get_sbc_by_name(name)
        if existing:
            raise ValueError(f"SBC with name '{name}' already exists")

        sbc_id = self.db.execute_insert(
            """
            INSERT INTO sbcs (name, project, description, ssh_user)
            VALUES (?, ?, ?, ?)
            """,
            (name, project, description, ssh_user),
        )

        self._audit_log("create", "sbc", sbc_id, name, f"Created SBC: {name}")

        return self.get_sbc(sbc_id)

    def get_sbc(self, sbc_id: int) -> Optional[SBC]:
        """Get SBC by ID with all related objects."""
        row = self.db.execute_one("SELECT * FROM sbcs WHERE id = ?", (sbc_id,))
        if not row:
            return None

        sbc = SBC.from_row(row)
        self._load_sbc_relations(sbc)
        return sbc

    def get_sbc_by_name(self, name: str) -> Optional[SBC]:
        """Get SBC by name with all related objects."""
        row = self.db.execute_one("SELECT * FROM sbcs WHERE name = ?", (name,))
        if not row:
            return None

        sbc = SBC.from_row(row)
        self._load_sbc_relations(sbc)
        return sbc

    def _load_sbc_relations(self, sbc: SBC) -> None:
        """Load related objects for an SBC."""
        # Load serial ports
        rows = self.db.execute("SELECT * FROM serial_ports WHERE sbc_id = ?", (sbc.id,))
        sbc.serial_ports = [SerialPort.from_row(r) for r in rows]

        # Load network addresses
        rows = self.db.execute(
            "SELECT * FROM network_addresses WHERE sbc_id = ?", (sbc.id,)
        )
        sbc.network_addresses = [NetworkAddress.from_row(r) for r in rows]

        # Load power plug
        row = self.db.execute_one(
            "SELECT * FROM power_plugs WHERE sbc_id = ?", (sbc.id,)
        )
        if row:
            sbc.power_plug = PowerPlug.from_row(row)

    def list_sbcs(
        self,
        project: Optional[str] = None,
        status: Optional[Status] = None,
    ) -> list[SBC]:
        """
        List all SBCs with optional filters.

        Args:
            project: Filter by project name
            status: Filter by status

        Returns:
            List of SBC instances
        """
        sql = "SELECT * FROM sbcs WHERE 1=1"
        params = []

        if project:
            sql += " AND project = ?"
            params.append(project)

        if status:
            sql += " AND status = ?"
            params.append(status.value)

        sql += " ORDER BY name"

        rows = self.db.execute(sql, tuple(params))
        sbcs = []
        for row in rows:
            sbc = SBC.from_row(row)
            self._load_sbc_relations(sbc)
            sbcs.append(sbc)

        return sbcs

    def update_sbc(
        self,
        sbc_id: int,
        project: Optional[str] = None,
        description: Optional[str] = None,
        ssh_user: Optional[str] = None,
        status: Optional[Status] = None,
    ) -> Optional[SBC]:
        """Update SBC fields."""
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return None

        updates = []
        params = []

        if project is not None:
            updates.append("project = ?")
            params.append(project)

        if description is not None:
            updates.append("description = ?")
            params.append(description)

        if ssh_user is not None:
            updates.append("ssh_user = ?")
            params.append(ssh_user)

        if status is not None:
            updates.append("status = ?")
            params.append(status.value)

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE sbcs SET {', '.join(updates)} WHERE id = ?"
            params.append(sbc_id)
            self.db.execute_modify(sql, tuple(params))
            self._audit_log(
                "update", "sbc", sbc_id, sbc.name, f"Updated SBC: {sbc.name}"
            )

        return self.get_sbc(sbc_id)

    def delete_sbc(self, sbc_id: int) -> bool:
        """
        Delete SBC and all related records.

        Returns:
            True if deleted, False if not found
        """
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return False

        # Cascade delete handles related records
        count = self.db.execute_modify("DELETE FROM sbcs WHERE id = ?", (sbc_id,))
        if count > 0:
            self._audit_log(
                "delete", "sbc", sbc_id, sbc.name, f"Deleted SBC: {sbc.name}"
            )
            return True

        return False

    # --- Serial Port Operations ---

    def assign_serial_port(
        self,
        sbc_id: int,
        port_type: PortType,
        device_path: str,
        tcp_port: Optional[int] = None,
        baud_rate: int = 115200,
    ) -> SerialPort:
        """
        Assign a serial port to an SBC.

        Args:
            sbc_id: SBC ID
            port_type: Type of port (console, jtag, debug)
            device_path: Path to device (e.g., /dev/lab/sbc1-console)
            tcp_port: TCP port for ser2net (auto-assigned if None)
            baud_rate: Baud rate (default: 115200)

        Returns:
            Created SerialPort instance
        """
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            raise ValueError(f"SBC with ID {sbc_id} not found")

        # Auto-assign TCP port if not specified
        if tcp_port is None:
            tcp_port = self._next_tcp_port()

        # Insert or update (upsert)
        self.db.execute_modify(
            "DELETE FROM serial_ports WHERE sbc_id = ? AND port_type = ?",
            (sbc_id, port_type.value),
        )

        port_id = self.db.execute_insert(
            """
            INSERT INTO serial_ports
                (sbc_id, port_type, device_path, tcp_port, baud_rate)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sbc_id, port_type.value, device_path, tcp_port, baud_rate),
        )

        self._audit_log(
            "assign",
            "serial_port",
            port_id,
            sbc.name,
            f"Assigned {port_type.value} port {device_path} to {sbc.name}",
        )

        row = self.db.execute_one("SELECT * FROM serial_ports WHERE id = ?", (port_id,))
        return SerialPort.from_row(row)

    def remove_serial_port(self, sbc_id: int, port_type: PortType) -> bool:
        """Remove a serial port assignment."""
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return False

        count = self.db.execute_modify(
            "DELETE FROM serial_ports WHERE sbc_id = ? AND port_type = ?",
            (sbc_id, port_type.value),
        )

        if count > 0:
            self._audit_log(
                "remove",
                "serial_port",
                None,
                sbc.name,
                f"Removed {port_type.value} port from {sbc.name}",
            )
            return True

        return False

    def list_serial_ports(self) -> list[SerialPort]:
        """List all serial port assignments."""
        rows = self.db.execute("SELECT * FROM serial_ports ORDER BY sbc_id, port_type")
        return [SerialPort.from_row(r) for r in rows]

    def _next_tcp_port(self, base_port: int = 4000) -> int:
        """Get next available TCP port."""
        row = self.db.execute_one("SELECT MAX(tcp_port) as max_port FROM serial_ports")
        if row and row["max_port"]:
            return row["max_port"] + 1
        return base_port

    # --- Network Address Operations ---

    def set_network_address(
        self,
        sbc_id: int,
        address_type: AddressType,
        ip_address: str,
        mac_address: Optional[str] = None,
        hostname: Optional[str] = None,
    ) -> NetworkAddress:
        """Set network address for an SBC."""
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            raise ValueError(f"SBC with ID {sbc_id} not found")

        # Insert or update (upsert)
        self.db.execute_modify(
            "DELETE FROM network_addresses WHERE sbc_id = ? AND address_type = ?",
            (sbc_id, address_type.value),
        )

        addr_id = self.db.execute_insert(
            """
            INSERT INTO network_addresses
                (sbc_id, address_type, ip_address, mac_address, hostname)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sbc_id, address_type.value, ip_address, mac_address, hostname),
        )

        self._audit_log(
            "set",
            "network_address",
            addr_id,
            sbc.name,
            f"Set {address_type.value} address {ip_address} for {sbc.name}",
        )

        row = self.db.execute_one(
            "SELECT * FROM network_addresses WHERE id = ?", (addr_id,)
        )
        return NetworkAddress.from_row(row)

    def remove_network_address(self, sbc_id: int, address_type: AddressType) -> bool:
        """Remove network address from an SBC."""
        count = self.db.execute_modify(
            "DELETE FROM network_addresses WHERE sbc_id = ? AND address_type = ?",
            (sbc_id, address_type.value),
        )
        return count > 0

    # --- Power Plug Operations ---

    def assign_power_plug(
        self,
        sbc_id: int,
        plug_type: PlugType,
        address: str,
        plug_index: int = 1,
    ) -> PowerPlug:
        """Assign a power plug to an SBC."""
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            raise ValueError(f"SBC with ID {sbc_id} not found")

        # Remove existing plug assignment
        self.db.execute_modify("DELETE FROM power_plugs WHERE sbc_id = ?", (sbc_id,))

        plug_id = self.db.execute_insert(
            """
            INSERT INTO power_plugs (sbc_id, plug_type, address, plug_index)
            VALUES (?, ?, ?, ?)
            """,
            (sbc_id, plug_type.value, address, plug_index),
        )

        self._audit_log(
            "assign",
            "power_plug",
            plug_id,
            sbc.name,
            f"Assigned {plug_type.value} plug {address} to {sbc.name}",
        )

        row = self.db.execute_one("SELECT * FROM power_plugs WHERE id = ?", (plug_id,))
        return PowerPlug.from_row(row)

    def remove_power_plug(self, sbc_id: int) -> bool:
        """Remove power plug assignment from an SBC."""
        count = self.db.execute_modify(
            "DELETE FROM power_plugs WHERE sbc_id = ?", (sbc_id,)
        )
        return count > 0

    # --- Status Log ---

    def log_status(
        self,
        sbc_id: int,
        status: Status,
        details: Optional[str] = None,
    ) -> int:
        """
        Log a status change to the status_log table.

        Args:
            sbc_id: ID of the SBC
            status: New status value
            details: Optional details about the status change

        Returns:
            ID of the inserted log entry
        """
        return self.db.execute_insert(
            """
            INSERT INTO status_log (sbc_id, status, details)
            VALUES (?, ?, ?)
            """,
            (sbc_id, status.value, details),
        )

    def get_status_history(
        self,
        sbc_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get status history for an SBC or all SBCs.

        Args:
            sbc_id: Optional SBC ID to filter by
            limit: Maximum number of entries to return

        Returns:
            List of status log entries as dictionaries
        """
        if sbc_id:
            rows = self.db.execute_query(
                """
                SELECT sl.id, sl.sbc_id, s.name as sbc_name, sl.status,
                       sl.details, sl.logged_at
                FROM status_log sl
                JOIN sbcs s ON sl.sbc_id = s.id
                WHERE sl.sbc_id = ?
                ORDER BY sl.logged_at DESC
                LIMIT ?
                """,
                (sbc_id, limit),
            )
        else:
            rows = self.db.execute_query(
                """
                SELECT sl.id, sl.sbc_id, s.name as sbc_name, sl.status,
                       sl.details, sl.logged_at
                FROM status_log sl
                JOIN sbcs s ON sl.sbc_id = s.id
                ORDER BY sl.logged_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        return [
            {
                "id": row[0],
                "sbc_id": row[1],
                "sbc_name": row[2],
                "status": row[3],
                "details": row[4],
                "logged_at": row[5],
            }
            for row in rows
        ]

    def cleanup_old_status_logs(self, retention_days: int) -> int:
        """
        Delete status log entries older than retention period.

        Args:
            retention_days: Number of days to retain status logs

        Returns:
            Number of deleted entries
        """
        return self.db.execute_modify(
            """
            DELETE FROM status_log
            WHERE logged_at < datetime('now', ? || ' days')
            """,
            (f"-{retention_days}",),
        )

    def get_uptime(self, sbc_id: int) -> Optional[dict]:
        """
        Calculate uptime statistics for an SBC.

        Calculates the current uptime (time since last transition to online)
        and total uptime over the last 24 hours.

        Args:
            sbc_id: ID of the SBC

        Returns:
            Dictionary with uptime statistics, or None if no history
        """
        from datetime import datetime, timedelta

        # Get last online transition
        last_online_row = self.db.execute_one(
            """
            SELECT logged_at FROM status_log
            WHERE sbc_id = ? AND status = 'online'
            ORDER BY logged_at DESC
            LIMIT 1
            """,
            (sbc_id,),
        )

        # Get last offline transition
        last_offline_row = self.db.execute_one(
            """
            SELECT logged_at FROM status_log
            WHERE sbc_id = ? AND status IN ('offline', 'error')
            ORDER BY logged_at DESC
            LIMIT 1
            """,
            (sbc_id,),
        )

        # Get current SBC status
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return None

        result = {
            "sbc_id": sbc_id,
            "sbc_name": sbc.name,
            "current_status": sbc.status.value,
            "current_uptime_seconds": 0,
            "current_uptime_formatted": "0s",
            "uptime_24h_percent": 0.0,
        }

        now = datetime.now()

        # Calculate current uptime if online
        if sbc.status == Status.ONLINE and last_online_row:
            last_online = datetime.fromisoformat(last_online_row[0])
            # Check if we went offline after going online
            if last_offline_row:
                last_offline = datetime.fromisoformat(last_offline_row[0])
                if last_offline > last_online:
                    # We're currently offline
                    result["current_uptime_seconds"] = 0
                else:
                    # We're online since last_online
                    uptime = (now - last_online).total_seconds()
                    result["current_uptime_seconds"] = int(uptime)
            else:
                # Never went offline
                uptime = (now - last_online).total_seconds()
                result["current_uptime_seconds"] = int(uptime)

            result["current_uptime_formatted"] = self._format_duration(
                result["current_uptime_seconds"]
            )

        # Calculate 24h uptime percentage
        day_ago = now - timedelta(hours=24)
        rows = self.db.execute_query(
            """
            SELECT status, logged_at FROM status_log
            WHERE sbc_id = ? AND logged_at >= ?
            ORDER BY logged_at ASC
            """,
            (sbc_id, day_ago.isoformat()),
        )

        if rows:
            online_seconds = 0
            prev_status = None
            prev_time = day_ago

            for row in rows:
                status = row[0]
                time_str = row[1]
                log_time = datetime.fromisoformat(time_str)

                if prev_status == "online":
                    online_seconds += (log_time - prev_time).total_seconds()

                prev_status = status
                prev_time = log_time

            # Account for current status until now
            if prev_status == "online":
                online_seconds += (now - prev_time).total_seconds()

            total_seconds = 24 * 60 * 60
            result["uptime_24h_percent"] = round(
                (online_seconds / total_seconds) * 100, 2
            )

        return result

    def _format_duration(self, seconds: int) -> str:
        """Format duration in seconds to human-readable string."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            m = seconds // 60
            s = seconds % 60
            return f"{m}m {s}s"
        elif seconds < 86400:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h}h {m}m"
        else:
            d = seconds // 86400
            h = (seconds % 86400) // 3600
            return f"{d}d {h}h"

    # --- Audit Log ---

    def _audit_log(
        self,
        action: str,
        entity_type: str,
        entity_id: Optional[int],
        entity_name: Optional[str],
        details: str,
    ) -> None:
        """Log an action to the audit log."""
        self.db.execute_insert(
            """
            INSERT INTO audit_log (action, entity_type, entity_id, entity_name, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (action, entity_type, entity_id, entity_name, details),
        )


def get_manager(db_path: Path) -> ResourceManager:
    """
    Get a resource manager instance.

    Args:
        db_path: Path to database file

    Returns:
        Initialized ResourceManager instance
    """
    db = get_database(db_path)
    return ResourceManager(db)
