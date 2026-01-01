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
