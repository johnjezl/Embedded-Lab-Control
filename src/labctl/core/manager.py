"""
Resource manager for lab controller.

Provides CRUD operations for SBCs, serial ports, network addresses, and power plugs.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from labctl.core import audit
from labctl.core.database import Database, get_database
from labctl.core.models import (
    SBC,
    AddressType,
    Claim,
    ClaimConflict,
    ClaimNotFoundError,
    ClaimRequest,
    NetworkAddress,
    NotClaimantError,
    PlugType,
    PortType,
    PowerPlug,
    ReleaseReason,
    SDWireDevice,
    SerialDevice,
    SerialPort,
    Status,
    UnknownSBCError,
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

    def _load_serial_device(self, port: SerialPort) -> None:
        """Load the serial device for a port if it has one."""
        if port.serial_device_id:
            dev_row = self.db.execute_one(
                "SELECT * FROM serial_devices WHERE id = ?",
                (port.serial_device_id,),
            )
            if dev_row:
                port.serial_device = SerialDevice.from_row(dev_row)

    def _load_sbc_relations(self, sbc: SBC) -> None:
        """Load related objects for an SBC."""
        # Load serial ports
        rows = self.db.execute("SELECT * FROM serial_ports WHERE sbc_id = ?", (sbc.id,))
        sbc.serial_ports = [SerialPort.from_row(r) for r in rows]
        for port in sbc.serial_ports:
            self._load_serial_device(port)

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

        # Load SDWire assignment
        row = self.db.execute_one(
            """SELECT sd.* FROM sdwire_devices sd
               JOIN sdwire_assignments sa ON sa.sdwire_device_id = sd.id
               WHERE sa.sbc_id = ?""",
            (sbc.id,),
        )
        if row:
            sbc.sdwire = SDWireDevice.from_row(row)

    def _load_sbc_relations_batch(self, sbcs: list[SBC]) -> None:
        """Load related objects for many SBCs using batched queries."""
        if not sbcs:
            return

        sbc_by_id = {sbc.id: sbc for sbc in sbcs if sbc.id is not None}
        if not sbc_by_id:
            return

        for sbc in sbcs:
            sbc.serial_ports = []
            sbc.network_addresses = []
            sbc.power_plug = None
            sbc.sdwire = None

        placeholders = ",".join("?" for _ in sbc_by_id)
        sbc_ids = tuple(sbc_by_id.keys())

        serial_ports_by_device_id: dict[int, list[SerialPort]] = {}
        port_rows = self.db.execute(
            f"""
            SELECT * FROM serial_ports
            WHERE sbc_id IN ({placeholders})
            ORDER BY sbc_id, id
            """,
            sbc_ids,
        )
        for row in port_rows:
            port = SerialPort.from_row(row)
            sbc_by_id[port.sbc_id].serial_ports.append(port)
            if port.serial_device_id is not None:
                serial_ports_by_device_id.setdefault(port.serial_device_id, []).append(
                    port
                )

        if serial_ports_by_device_id:
            device_placeholders = ",".join("?" for _ in serial_ports_by_device_id)
            device_rows = self.db.execute(
                f"""
                SELECT * FROM serial_devices
                WHERE id IN ({device_placeholders})
                """,
                tuple(serial_ports_by_device_id.keys()),
            )
            for row in device_rows:
                device = SerialDevice.from_row(row)
                for port in serial_ports_by_device_id.get(device.id, []):
                    port.serial_device = device

        address_rows = self.db.execute(
            f"""
            SELECT * FROM network_addresses
            WHERE sbc_id IN ({placeholders})
            ORDER BY sbc_id, id
            """,
            sbc_ids,
        )
        for row in address_rows:
            address = NetworkAddress.from_row(row)
            sbc_by_id[address.sbc_id].network_addresses.append(address)

        power_rows = self.db.execute(
            f"""
            SELECT * FROM power_plugs
            WHERE sbc_id IN ({placeholders})
            """,
            sbc_ids,
        )
        for row in power_rows:
            plug = PowerPlug.from_row(row)
            sbc_by_id[plug.sbc_id].power_plug = plug

        sdwire_rows = self.db.execute(
            f"""
            SELECT sd.*, sa.sbc_id AS assigned_sbc_id
            FROM sdwire_devices sd
            JOIN sdwire_assignments sa ON sa.sdwire_device_id = sd.id
            WHERE sa.sbc_id IN ({placeholders})
            """,
            sbc_ids,
        )
        for row in sdwire_rows:
            sbc_by_id[row["assigned_sbc_id"]].sdwire = SDWireDevice.from_row(row)

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
        sbcs = [SBC.from_row(row) for row in rows]
        self._load_sbc_relations_batch(sbcs)
        return sbcs

    def update_sbc(
        self,
        sbc_id: int,
        name: Optional[str] = None,
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

        if name is not None:
            updates.append("name = ?")
            params.append(name)

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
            old_name = sbc.name
            new_name = name if name else old_name
            self._audit_log(
                "update",
                "sbc",
                sbc_id,
                new_name,
                f"Updated SBC: {old_name}"
                + (f" (renamed to {new_name})" if name and name != old_name else ""),
            )

        return self.get_sbc(sbc_id)

    def delete_sbc(self, sbc_id: int, force: bool = False) -> bool:
        """
        Delete SBC and all related records.

        Args:
            sbc_id: SBC database ID
            force: If True, delete even when an active claim exists
                (the claim rows cascade-delete with the SBC).

        Returns:
            True if deleted, False if not found

        Raises:
            ClaimConflict: If an active claim exists and force=False.
        """
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return False

        if not force:
            active = self.get_active_claim(sbc.name)
            if active is not None:
                raise ClaimConflict(active)

        # Cascade delete handles related records (including claims)
        count = self.db.execute_modify("DELETE FROM sbcs WHERE id = ?", (sbc_id,))
        if count > 0:
            self._audit_log(
                "delete", "sbc", sbc_id, sbc.name, f"Deleted SBC: {sbc.name}"
            )
            return True

        return False

    # --- Serial Device Operations ---

    def create_serial_device(
        self,
        name: str,
        usb_path: str,
        vendor: Optional[str] = None,
        model: Optional[str] = None,
        serial_number: Optional[str] = None,
    ) -> SerialDevice:
        """Register a USB-serial adapter."""
        device_id = self.db.execute_insert(
            """
            INSERT INTO serial_devices (name, usb_path, vendor, model, serial_number)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, usb_path, vendor, model, serial_number),
        )
        self._audit_log(
            "create",
            "serial_device",
            device_id,
            name,
            f"Registered serial device: {name} ({usb_path})",
        )
        row = self.db.execute_one(
            "SELECT * FROM serial_devices WHERE id = ?", (device_id,)
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve serial device {device_id}")
        return SerialDevice.from_row(row)

    def get_serial_device(self, device_id: int) -> Optional[SerialDevice]:
        """Get serial device by ID."""
        row = self.db.execute_one(
            "SELECT * FROM serial_devices WHERE id = ?", (device_id,)
        )
        return SerialDevice.from_row(row) if row else None

    def get_serial_device_by_name(self, name: str) -> Optional[SerialDevice]:
        """Get serial device by name."""
        row = self.db.execute_one(
            "SELECT * FROM serial_devices WHERE name = ?", (name,)
        )
        return SerialDevice.from_row(row) if row else None

    def list_serial_devices(self) -> list[SerialDevice]:
        """List all registered serial devices."""
        rows = self.db.execute("SELECT * FROM serial_devices ORDER BY name")
        return [SerialDevice.from_row(r) for r in rows]

    def rename_serial_device(
        self, device_id: int, new_name: str
    ) -> Optional[SerialDevice]:
        """Rename a serial device."""
        device = self.get_serial_device(device_id)
        if not device:
            return None
        self.db.execute_modify(
            "UPDATE serial_devices SET name = ? WHERE id = ?", (new_name, device_id)
        )
        self._audit_log(
            "update",
            "serial_device",
            device_id,
            new_name,
            f"Renamed serial device: {device.name} -> {new_name}",
        )
        return self.get_serial_device(device_id)

    def delete_serial_device(self, device_id: int) -> bool:
        """Delete a serial device. Raises ValueError if still assigned to a port."""
        device = self.get_serial_device(device_id)
        if not device:
            return False

        # Check if in use
        row = self.db.execute_one(
            "SELECT id FROM serial_ports WHERE serial_device_id = ?", (device_id,)
        )
        if row:
            raise ValueError(
                f"Serial device '{device.name}' is still assigned to a port. "
                "Remove the port assignment first."
            )

        count = self.db.execute_modify(
            "DELETE FROM serial_devices WHERE id = ?", (device_id,)
        )
        if count > 0:
            self._audit_log(
                "delete",
                "serial_device",
                device_id,
                device.name,
                f"Deleted serial device: {device.name}",
            )
            return True
        return False

    # --- Serial Port Operations ---

    def _resolve_serial_device_id(self, device_path: str) -> Optional[int]:
        """Resolve a device_path like "/dev/lab/port-2-3" to a serial_device_id.

        Returns None if the path doesn't match the /dev/lab/<name>
        convention, or no registered device has that name.
        """
        if not device_path:
            return None
        # Accept "/dev/lab/<name>" and also bare "<name>" (legacy rows).
        prefix = "/dev/lab/"
        if device_path.startswith(prefix):
            name = device_path[len(prefix) :]
        elif "/" not in device_path:
            name = device_path
        else:
            return None
        if not name:
            return None
        row = self.db.execute_one(
            "SELECT id FROM serial_devices WHERE name = ?", (name,)
        )
        return row["id"] if row else None

    def repair_serial_port_links(self, apply: bool = False) -> list[dict]:
        """Backfill NULL `serial_device_id` on serial_ports from device_path.

        Args:
            apply: If True, perform the UPDATE. Otherwise return the
                planned changes without writing.

        Returns:
            List of dicts describing each repair: {port_id, sbc_id,
            alias, device_path, resolved_device_id, resolved_name,
            status}. Status is "repaired", "applied" (if apply=True and
            write succeeded), "unresolvable" (no matching serial_device),
            or "skipped" (FK was already set — included only if you
            query via a broader scan; this method only returns rows it
            had to consider).
        """
        rows = self.db.execute(
            "SELECT id, sbc_id, alias, device_path, serial_device_id "
            "FROM serial_ports WHERE serial_device_id IS NULL"
        )
        results: list[dict] = []
        for r in rows:
            resolved_id = self._resolve_serial_device_id(r["device_path"])
            entry = {
                "port_id": r["id"],
                "sbc_id": r["sbc_id"],
                "alias": r["alias"],
                "device_path": r["device_path"],
                "resolved_device_id": resolved_id,
                "resolved_name": None,
                "status": "unresolvable" if resolved_id is None else "repaired",
            }
            if resolved_id is not None:
                name_row = self.db.execute_one(
                    "SELECT name FROM serial_devices WHERE id = ?",
                    (resolved_id,),
                )
                entry["resolved_name"] = name_row["name"] if name_row else None
                if apply:
                    self.db.execute_modify(
                        "UPDATE serial_ports SET serial_device_id = ? WHERE id = ?",
                        (resolved_id, r["id"]),
                    )
                    entry["status"] = "applied"
            results.append(entry)
        return results

    def assign_serial_port(
        self,
        sbc_id: int,
        port_type: PortType,
        device_path: str,
        tcp_port: Optional[int] = None,
        baud_rate: int = 115200,
        alias: Optional[str] = None,
        serial_device_id: Optional[int] = None,
    ) -> SerialPort:
        """
        Assign a serial port to an SBC.

        Args:
            sbc_id: SBC ID
            port_type: Type of port (console, jtag, debug)
            device_path: Path to device (e.g., /dev/lab/port-1)
            tcp_port: TCP port for ser2net (auto-assigned if None)
            baud_rate: Baud rate (default: 115200)
            alias: Human-friendly name for this assignment
            serial_device_id: FK to serial_devices table
        """
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            raise ValueError(f"SBC with ID {sbc_id} not found")

        # Validate alias uniqueness
        if alias:
            existing = self.db.execute_one(
                "SELECT id, sbc_id, port_type FROM serial_ports WHERE alias = ?",
                (alias,),
            )
            if existing and (
                existing["sbc_id"] != sbc_id or existing["port_type"] != port_type.value
            ):
                raise ValueError(f"Alias '{alias}' is already in use")

        # Validate serial device exists
        if serial_device_id:
            dev = self.db.execute_one(
                "SELECT id FROM serial_devices WHERE id = ?", (serial_device_id,)
            )
            if not dev:
                raise ValueError(f"Serial device with ID {serial_device_id} not found")
        else:
            # Auto-resolve from device_path so callers that only provide a
            # /dev/lab/<name> path (CLI without --serial-device, MCP tools,
            # REST API) still populate the two-tier link. Without this, the
            # port works at the device layer but `labctl serial list` shows
            # the adapter as unassigned. Silently leave NULL if no match —
            # paths outside /dev/lab (raw /dev/ttyUSB*, etc.) are legal.
            resolved = self._resolve_serial_device_id(device_path)
            if resolved is not None:
                serial_device_id = resolved

        # Auto-assign TCP port if not specified
        if tcp_port is None:
            tcp_port = self._next_tcp_port()

        # Atomic upsert
        port_id = self.db.execute_insert(
            """
            INSERT INTO serial_ports
                (sbc_id, port_type, device_path, tcp_port, baud_rate, alias, serial_device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (sbc_id, port_type) DO UPDATE SET
                device_path = excluded.device_path,
                tcp_port = excluded.tcp_port,
                baud_rate = excluded.baud_rate,
                alias = excluded.alias,
                serial_device_id = excluded.serial_device_id
            """,
            (
                sbc_id,
                port_type.value,
                device_path,
                tcp_port,
                baud_rate,
                alias,
                serial_device_id,
            ),
        )

        details = f"Assigned {port_type.value} port {device_path} to {sbc.name}"
        if alias:
            details += f" (alias: {alias})"
        self._audit_log("assign", "serial_port", port_id, sbc.name, details)

        row = self.db.execute_one(
            "SELECT * FROM serial_ports WHERE sbc_id = ? AND port_type = ?",
            (sbc_id, port_type.value),
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve serial port for {sbc.name}")
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
        ports = [SerialPort.from_row(r) for r in rows]
        for port in ports:
            self._load_serial_device(port)
        return ports

    def get_serial_port_by_alias(self, alias: str) -> Optional[SerialPort]:
        """Get a serial port assignment by its alias."""
        row = self.db.execute_one(
            "SELECT * FROM serial_ports WHERE alias = ?", (alias,)
        )
        if not row:
            return None
        port = SerialPort.from_row(row)
        self._load_serial_device(port)
        return port

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

        # Atomic upsert
        addr_id = self.db.execute_insert(
            """
            INSERT INTO network_addresses
                (sbc_id, address_type, ip_address, mac_address, hostname)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sbc_id, address_type) DO UPDATE SET
                ip_address = excluded.ip_address,
                mac_address = excluded.mac_address,
                hostname = excluded.hostname
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
            "SELECT * FROM network_addresses WHERE sbc_id = ? AND address_type = ?",
            (sbc_id, address_type.value),
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve network address for {sbc.name}")
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

        # Atomic upsert
        plug_id = self.db.execute_insert(
            """
            INSERT INTO power_plugs (sbc_id, plug_type, address, plug_index)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (sbc_id) DO UPDATE SET
                plug_type = excluded.plug_type,
                address = excluded.address,
                plug_index = excluded.plug_index
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

        row = self.db.execute_one(
            "SELECT * FROM power_plugs WHERE sbc_id = ?", (sbc_id,)
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve power plug for {sbc.name}")
        return PowerPlug.from_row(row)

    def remove_power_plug(self, sbc_id: int) -> bool:
        """Remove power plug assignment from an SBC."""
        count = self.db.execute_modify(
            "DELETE FROM power_plugs WHERE sbc_id = ?", (sbc_id,)
        )
        return count > 0

    # --- SDWire Device Operations ---

    def create_sdwire_device(
        self,
        name: str,
        serial_number: str,
        device_type: str = "sdwirec",
    ) -> SDWireDevice:
        """Register an SDWire SD card multiplexer device."""
        device_id = self.db.execute_insert(
            """INSERT INTO sdwire_devices (name, serial_number, device_type)
               VALUES (?, ?, ?)""",
            (name, serial_number, device_type),
        )
        self._audit_log(
            "create",
            "sdwire_device",
            device_id,
            name,
            f"Registered SDWire device: {name} ({serial_number})",
        )
        row = self.db.execute_one(
            "SELECT * FROM sdwire_devices WHERE id = ?", (device_id,)
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve SDWire device {device_id}")
        return SDWireDevice.from_row(row)

    def get_sdwire_device(self, device_id: int) -> Optional[SDWireDevice]:
        """Get SDWire device by ID."""
        row = self.db.execute_one(
            "SELECT * FROM sdwire_devices WHERE id = ?", (device_id,)
        )
        return SDWireDevice.from_row(row) if row else None

    def get_sdwire_device_by_name(self, name: str) -> Optional[SDWireDevice]:
        """Get SDWire device by name."""
        row = self.db.execute_one(
            "SELECT * FROM sdwire_devices WHERE name = ?", (name,)
        )
        return SDWireDevice.from_row(row) if row else None

    def list_sdwire_devices(self) -> list[SDWireDevice]:
        """List all registered SDWire devices."""
        rows = self.db.execute("SELECT * FROM sdwire_devices ORDER BY name")
        return [SDWireDevice.from_row(r) for r in rows]

    def delete_sdwire_device(self, device_id: int) -> bool:
        """Delete an SDWire device. Raises ValueError if still assigned."""
        device = self.get_sdwire_device(device_id)
        if not device:
            return False

        row = self.db.execute_one(
            "SELECT id FROM sdwire_assignments WHERE sdwire_device_id = ?", (device_id,)
        )
        if row:
            raise ValueError(
                f"SDWire device '{device.name}' is still assigned to an SBC. "
                "Unassign it first."
            )

        count = self.db.execute_modify(
            "DELETE FROM sdwire_devices WHERE id = ?", (device_id,)
        )
        if count > 0:
            self._audit_log(
                "delete",
                "sdwire_device",
                device_id,
                device.name,
                f"Deleted SDWire device: {device.name}",
            )
            return True
        return False

    def assign_sdwire(self, sbc_id: int, sdwire_device_id: int) -> None:
        """Assign an SDWire device to an SBC."""
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            raise ValueError(f"SBC with ID {sbc_id} not found")

        device = self.get_sdwire_device(sdwire_device_id)
        if not device:
            raise ValueError(f"SDWire device with ID {sdwire_device_id} not found")

        # Atomic upsert
        self.db.execute_insert(
            """INSERT INTO sdwire_assignments (sbc_id, sdwire_device_id) VALUES (?, ?)
               ON CONFLICT (sbc_id) DO UPDATE SET sdwire_device_id = excluded.sdwire_device_id""",
            (sbc_id, sdwire_device_id),
        )

        self._audit_log(
            "assign",
            "sdwire",
            sdwire_device_id,
            sbc.name,
            f"Assigned SDWire '{device.name}' to {sbc.name}",
        )

    def unassign_sdwire(self, sbc_id: int) -> bool:
        """Remove SDWire assignment from an SBC."""
        count = self.db.execute_modify(
            "DELETE FROM sdwire_assignments WHERE sbc_id = ?", (sbc_id,)
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
            rows = self.db.execute(
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
            rows = self.db.execute(
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
                "id": row["id"],
                "sbc_id": row["sbc_id"],
                "sbc_name": row["sbc_name"],
                "status": row["status"],
                "details": row["details"],
                "logged_at": row["logged_at"],
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
            last_online = datetime.fromisoformat(last_online_row["logged_at"])
            # Check if we went offline after going online
            if last_offline_row:
                last_offline = datetime.fromisoformat(last_offline_row["logged_at"])
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
        rows = self.db.execute(
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
                status = row["status"]
                time_str = row["logged_at"]
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

    # --- Claim Operations ---

    # Microsecond precision so short-duration expiry comparisons don't
    # collapse to a single second. SQLite stores these as TEXT and the
    # ISO-8601-ish layout compares lexically in the same order as datetimes.
    _TS_FMT = "%Y-%m-%d %H:%M:%S.%f"

    @classmethod
    def _fmt_ts(cls, dt: datetime) -> str:
        return dt.strftime(cls._TS_FMT)

    def _load_pending_requests(self, claim: Claim) -> None:
        rows = self.db.execute(
            """
            SELECT * FROM claim_requests
            WHERE claim_id = ? AND acknowledged = 0
            ORDER BY requested_at ASC
            """,
            (claim.id,),
        )
        claim.pending_requests = [ClaimRequest.from_row(r) for r in rows]

    def _require_sbc_id(self, sbc_name: str) -> int:
        sbc = self.get_sbc_by_name(sbc_name)
        if sbc is None:
            raise UnknownSBCError(f"SBC '{sbc_name}' does not exist")
        return sbc.id

    def expire_stale_claims(self, grace_seconds: int = 60) -> int:
        """Mark claims past their deadline (+ grace) as released.

        Returns the number of claims released by this sweep. Safe to call
        repeatedly; only unreleased rows with an elapsed deadline are touched.
        """
        cutoff = self._fmt_ts(datetime.now() - timedelta(seconds=grace_seconds))
        return self.db.execute_modify(
            """
            UPDATE claims
            SET released_at = CURRENT_TIMESTAMP,
                release_reason = ?,
                released_by = 'system'
            WHERE released_at IS NULL AND expires_at < ?
            """,
            (ReleaseReason.EXPIRED.value, cutoff),
        )

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check whether a process is alive (works on Linux/macOS)."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it — still alive.
            return True
        except OSError:
            return False

    def release_dead_sessions(self, grace_seconds: int = 60) -> int:
        """Release claims whose MCP stdio session process has exited.

        Parses ``mcp-stdio:<pid>-<epoch>`` session IDs. If the PID is no
        longer alive **and** the claim's deadline + grace has passed, the
        claim is released as ``session-lost``. Claims with other
        ``session_kind`` values (cli, web) are skipped.
        """
        rows = self.db.execute(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.released_at IS NULL AND c.session_kind = 'mcp-stdio'
            """
        )
        released = 0
        cutoff = datetime.now() - timedelta(seconds=grace_seconds)
        for row in rows:
            claim = Claim.from_row(row)
            # Parse "mcp-stdio:<pid>-<epoch>"
            try:
                payload = claim.session_id.split(":", 1)[1]
                pid = int(payload.split("-", 1)[0])
            except (IndexError, ValueError):
                continue

            if self._is_pid_alive(pid):
                continue

            # PID is dead — but only release if past grace
            if claim.expires_at and claim.expires_at > cutoff:
                continue

            self.db.execute_modify(
                """
                UPDATE claims
                SET released_at = CURRENT_TIMESTAMP,
                    release_reason = ?,
                    released_by = 'system'
                WHERE id = ?
                """,
                (ReleaseReason.SESSION_LOST.value, claim.id),
            )
            logger.info(
                "Released claim on '%s' (dead session pid=%d, agent=%s)",
                claim.sbc_name,
                pid,
                claim.agent_name,
            )
            self._audit_log(
                "session_lost",
                "sbc",
                claim.sbc_id,
                claim.sbc_name,
                f"Session pid={pid} died, claim released "
                f"(was held by {claim.agent_name})",
            )
            released += 1
        return released

    def prune_released_claims(self, older_than_days: int = 30) -> int:
        """Delete released claim rows older than the retention threshold.

        Only rows with a non-NULL ``released_at`` are eligible. Active
        (unreleased) claims are never pruned.
        """
        cutoff = self._fmt_ts(datetime.now() - timedelta(days=older_than_days))
        count = self.db.execute_modify(
            """
            DELETE FROM claims
            WHERE released_at IS NOT NULL AND released_at < ?
            """,
            (cutoff,),
        )
        if count:
            logger.info(
                "Pruned %d released claims older than %d days", count, older_than_days
            )
        return count

    def prune_activity_events(self, older_than_days: int = 30) -> int:
        """Delete audit events older than the retention threshold."""
        count = audit.prune_old_events(self.db, older_than_days=older_than_days)
        if count:
            logger.info(
                "Pruned %d activity events older than %d days",
                count,
                older_than_days,
            )
        return count

    def get_claim_metrics(self) -> dict:
        """Aggregate statistics across all claims (active and released)."""
        rows = self.db.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN released_at IS NULL THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN release_reason = 'released'       THEN 1 ELSE 0 END) AS released,
                SUM(CASE WHEN release_reason = 'expired'        THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN release_reason = 'force-released' THEN 1 ELSE 0 END) AS force_released,
                SUM(CASE WHEN release_reason = 'session-lost'   THEN 1 ELSE 0 END) AS session_lost,
                AVG(CASE WHEN released_at IS NOT NULL
                    THEN CAST(
                        (julianday(released_at) - julianday(acquired_at))
                        * 86400 AS INTEGER)
                    ELSE NULL END)                              AS avg_duration_seconds
            FROM claims
            """
        )
        row = rows[0] if rows else None
        if not row:
            return {
                "total": 0,
                "active": 0,
                "released": 0,
                "expired": 0,
                "force_released": 0,
                "session_lost": 0,
                "avg_duration_seconds": None,
            }
        return {
            "total": row["total"] or 0,
            "active": row["active"] or 0,
            "released": row["released"] or 0,
            "expired": row["expired"] or 0,
            "force_released": row["force_released"] or 0,
            "session_lost": row["session_lost"] or 0,
            "avg_duration_seconds": (
                round(row["avg_duration_seconds"])
                if row["avg_duration_seconds"] is not None
                else None
            ),
        }

    def claim_sbc(
        self,
        sbc_name: str,
        agent_name: str,
        session_id: str,
        session_kind: str,
        duration_seconds: int,
        reason: str,
        context: Optional[dict] = None,
        grace_seconds: int = 60,
    ) -> Claim:
        """Acquire an exclusive claim on an SBC.

        Runs an expire-stale sweep inside the acquisition transaction so
        the partial unique index can't block a legitimate caller behind a
        claim whose deadline has already passed.
        """
        sbc_id = self._require_sbc_id(sbc_name)
        context_json = json.dumps(context) if context else None
        cutoff = self._fmt_ts(datetime.now() - timedelta(seconds=grace_seconds))

        with self.db.connect() as conn:
            # Release any expired claims on this SBC within the same
            # transaction so the UNIQUE index reflects current reality.
            conn.execute(
                """
                UPDATE claims
                SET released_at = CURRENT_TIMESTAMP,
                    release_reason = ?,
                    released_by = 'system'
                WHERE released_at IS NULL AND expires_at < ?
                """,
                (ReleaseReason.EXPIRED.value, cutoff),
            )

            # Check for active claim after the sweep
            cursor = conn.execute(
                """
                SELECT c.*, s.name AS sbc_name
                FROM claims c
                JOIN sbcs s ON s.id = c.sbc_id
                WHERE c.sbc_id = ? AND c.released_at IS NULL
                """,
                (sbc_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                raise ClaimConflict(Claim.from_row(existing))

            now = datetime.now()
            expires_at = now + timedelta(seconds=duration_seconds)
            cursor = conn.execute(
                """
                INSERT INTO claims (
                    sbc_id, agent_name, session_id, session_kind, reason,
                    context_json, acquired_at, duration_seconds,
                    last_activity, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sbc_id,
                    agent_name,
                    session_id,
                    session_kind,
                    reason,
                    context_json,
                    self._fmt_ts(now),
                    duration_seconds,
                    self._fmt_ts(now),
                    self._fmt_ts(expires_at),
                ),
            )
            claim_id = cursor.lastrowid
            cursor = conn.execute(
                """
                SELECT c.*, s.name AS sbc_name
                FROM claims c JOIN sbcs s ON s.id = c.sbc_id
                WHERE c.id = ?
                """,
                (claim_id,),
            )
            claim = Claim.from_row(cursor.fetchone())

        self._audit_log(
            "claim",
            "sbc",
            sbc_id,
            sbc_name,
            f"Claimed by {agent_name} ({session_kind}) "
            f"for {duration_seconds}s: {reason}",
        )
        return claim

    def get_active_claim(self, sbc_name: str) -> Optional[Claim]:
        """Return the active claim on an SBC, or None if free."""
        sbc_id = self._require_sbc_id(sbc_name)
        row = self.db.execute_one(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.sbc_id = ? AND c.released_at IS NULL
            """,
            (sbc_id,),
        )
        if not row:
            return None
        claim = Claim.from_row(row)
        # If materialized deadline passed, treat as free — the next sweep
        # will commit the release. Don't return a zombie as active.
        if not claim.is_active:
            return None
        self._load_pending_requests(claim)
        return claim

    def list_active_claims(self) -> list[Claim]:
        """All currently active claims across the lab."""
        rows = self.db.execute(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.released_at IS NULL
            ORDER BY c.acquired_at ASC
            """
        )
        claims = []
        for row in rows:
            claim = Claim.from_row(row)
            if claim.is_active:
                self._load_pending_requests(claim)
                claims.append(claim)
        return claims

    def list_claim_history(self, sbc_name: str, limit: int = 10) -> list[Claim]:
        """Past (released) claims for a specific SBC, newest first."""
        sbc_id = self._require_sbc_id(sbc_name)
        rows = self.db.execute(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.sbc_id = ? AND c.released_at IS NOT NULL
            ORDER BY c.released_at DESC, c.id DESC
            LIMIT ?
            """,
            (sbc_id, limit),
        )
        return [Claim.from_row(r) for r in rows]

    def release_claim(self, sbc_name: str, session_id: str) -> Claim:
        """Release the active claim held by ``session_id`` on an SBC."""
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        if claim.session_id != session_id:
            raise NotClaimantError(
                f"Claim on '{sbc_name}' is held by a different session"
            )
        self.db.execute_modify(
            """
            UPDATE claims
            SET released_at = CURRENT_TIMESTAMP,
                release_reason = ?,
                released_by = ?
            WHERE id = ?
            """,
            (ReleaseReason.RELEASED.value, claim.agent_name, claim.id),
        )
        self._audit_log(
            "release",
            "sbc",
            claim.sbc_id,
            sbc_name,
            f"Released by {claim.agent_name}",
        )
        # Re-fetch fresh state so callers see released_at populated.
        return self._get_claim_by_id(claim.id)

    def force_release_claim(
        self, sbc_name: str, reason: str, released_by: str = "operator"
    ) -> Claim:
        """Operator override — forcibly release the active claim.

        Differs from :meth:`release_claim` in that no session-match check
        applies. The reason is appended to the audit log prominently.
        """
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        self.db.execute_modify(
            """
            UPDATE claims
            SET released_at = CURRENT_TIMESTAMP,
                release_reason = ?,
                released_by = ?
            WHERE id = ?
            """,
            (ReleaseReason.FORCE_RELEASED.value, released_by, claim.id),
        )
        self._audit_log(
            "force_release",
            "sbc",
            claim.sbc_id,
            sbc_name,
            f"Force-released by {released_by} "
            f"(was held by {claim.agent_name}): {reason}",
        )
        return self._get_claim_by_id(claim.id)

    def renew_claim(
        self,
        sbc_name: str,
        session_id: str,
        duration_seconds: Optional[int] = None,
    ) -> Claim:
        """Extend an active claim's deadline.

        If ``duration_seconds`` is omitted the previous duration is reused.
        ``last_activity`` and ``expires_at`` are both advanced to "now" and
        "now + duration_seconds" respectively.
        """
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        if claim.session_id != session_id:
            raise NotClaimantError(
                f"Claim on '{sbc_name}' is held by a different session"
            )
        new_duration = (
            duration_seconds if duration_seconds is not None else claim.duration_seconds
        )
        now = datetime.now()
        new_expiry = now + timedelta(seconds=new_duration)
        self.db.execute_modify(
            """
            UPDATE claims
            SET duration_seconds = ?,
                last_activity = ?,
                expires_at = ?,
                renewal_count = renewal_count + 1
            WHERE id = ?
            """,
            (new_duration, self._fmt_ts(now), self._fmt_ts(new_expiry), claim.id),
        )
        return self._get_claim_by_id(claim.id)

    def heartbeat_claim(self, sbc_name: str, session_id: str) -> bool:
        """Advance last_activity and expires_at on the claim, if any.

        Called from every claimant tool path (reads and writes). Silent
        no-op when no claim exists or a different session holds it, so
        that non-claim-aware callers don't need special handling.
        """
        claim = self.get_active_claim(sbc_name)
        if claim is None or claim.session_id != session_id:
            return False
        now = datetime.now()
        new_expiry = now + timedelta(seconds=claim.duration_seconds)
        self.db.execute_modify(
            """
            UPDATE claims
            SET last_activity = ?, expires_at = ?
            WHERE id = ?
            """,
            (self._fmt_ts(now), self._fmt_ts(new_expiry), claim.id),
        )
        return True

    def record_release_request(
        self, sbc_name: str, requested_by: str, reason: str
    ) -> ClaimRequest:
        """Record a polite release request against the active claim."""
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        request_id = self.db.execute_insert(
            """
            INSERT INTO claim_requests (claim_id, requested_by, reason)
            VALUES (?, ?, ?)
            """,
            (claim.id, requested_by, reason),
        )
        row = self.db.execute_one(
            "SELECT * FROM claim_requests WHERE id = ?", (request_id,)
        )
        return ClaimRequest.from_row(row)

    def _get_claim_by_id(self, claim_id: int) -> Optional[Claim]:
        row = self.db.execute_one(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.id = ?
            """,
            (claim_id,),
        )
        if not row:
            return None
        claim = Claim.from_row(row)
        if claim.released_at is None:
            self._load_pending_requests(claim)
        return claim

    # --- Audit Log ---

    def _audit_log(
        self,
        action: str,
        entity_type: str,
        entity_id: Optional[int],
        entity_name: Optional[str],
        details: str,
        result: str = "ok",
    ) -> None:
        """Log an action to the activity stream.

        Thin wrapper over `labctl.core.audit.emit()` that preserves the
        legacy positional-string-details calling convention used across
        the manager. `actor`, `source`, and `claim_id` are pulled from
        the audit contextvars.
        """
        audit.emit(
            self.db,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            result=result,
            details={"message": details} if details else None,
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
