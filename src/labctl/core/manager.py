"""
Resource manager for lab controller.

Provides CRUD operations for SBCs, serial ports, network addresses, and power plugs.
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from labctl.core import audit, timeutil
from labctl.core.database import DEFAULT_TIMEOUT_SECONDS as DEFAULT_DB_TIMEOUT
from labctl.core.database import Database, get_database
from labctl.core.models import (
    POST_POWER_REFUSED,
    SBC,
    Actuator,
    ActuatorChannel,
    ActuatorKind,
    AddressType,
    Binding,
    ChannelState,
    Claim,
    ClaimActor,
    ClaimConflict,
    ClaimNotFoundError,
    ClaimRequest,
    ClaimRequiredError,
    DesiredState,
    DriverName,
    NetworkAddress,
    NotClaimantError,
    NotOperatorError,
    PlugType,
    PortType,
    PowerPlug,
    ReleaseReason,
    SamplePhase,
    SDWireDevice,
    SerialDevice,
    SerialPort,
    ShapeMode,
    Status,
    UnknownSBCError,
)

logger = logging.getLogger(__name__)


def _norm_usb_id(value: str) -> str:
    """``1A86``, ``0x1a86`` and ``0x01a86`` name the same USB VID/PID."""
    value = value.strip()
    try:
        return format(int(value, 16), "04x")
    except ValueError:
        return value.lower()


def outlet_key(plug_type, address: str, plug_index: int) -> tuple:
    """A plug outlet as the sharing rule compares it: type, address text
    (trimmed, case-folded) and outlet index. Two different addresses for
    one plug (a hostname and its IP) are not recognised as the same."""
    plug_type = getattr(plug_type, "value", plug_type)
    index = plug_index if plug_index is not None else 1
    return (plug_type, (address or "").strip().lower(), index)


def outlets_overlap(a: tuple, b: tuple) -> bool:
    """Whether switching outlet ``a`` switches outlet ``b`` (keys from
    :func:`outlet_key`). An index below 1 (rows saved before assignment
    refused it) addresses the whole device on Tasmota (Power0), so it
    overlaps every outlet on that address."""
    return a[:2] == b[:2] and (a[2] == b[2] or a[2] < 1 or b[2] < 1)


def plug_outlet_key(plug) -> tuple:
    """:func:`outlet_key` of a PowerPlug."""
    return outlet_key(plug.plug_type, plug.address, plug.plug_index)


class ResourceManager:
    """Manager for lab resources (SBCs, ports, etc.)."""

    def __init__(self, db: Database, *, base_tcp_port: int = 4000):
        """
        Initialize resource manager.

        Args:
            db: Initialized database instance
            base_tcp_port: Lowest TCP port auto-assignment gives a serial
                port (``serial.base_tcp_port``)
        """
        self.db = db
        self.base_tcp_port = base_tcp_port
        # cleanup_old_status_logs: what status_log looked like after the
        # last prune, so an hourly call with nothing new to prune is cheap.
        self._prune_state: Optional[tuple] = None

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
        power_cycle_delay_seconds: Optional[float] = None,
    ) -> Optional[SBC]:
        """Update SBC fields.

        ``power_cycle_delay_seconds`` accepts 0 to ``MAX_CYCLE_DELAY_SECONDS``
        to set (ValueError otherwise, before anything is written), or a
        negative value as the sentinel for "clear back to default".
        """
        sbc = self.get_sbc(sbc_id)
        if not sbc:
            return None

        updates = []
        params = []
        # Human-readable per-field summaries for the audit log.
        changes: list[str] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
            changes.append(f"name: {sbc.name!r} -> {name!r}")

        if project is not None:
            updates.append("project = ?")
            params.append(project)
            changes.append(f"project={project!r}")

        if description is not None:
            updates.append("description = ?")
            params.append(description)
            changes.append(f"description={description!r}")

        if ssh_user is not None:
            updates.append("ssh_user = ?")
            params.append(ssh_user)
            changes.append(f"ssh_user={ssh_user!r}")

        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
            changes.append(f"status={status.value}")

        if power_cycle_delay_seconds is not None:
            updates.append("power_cycle_delay_seconds = ?")
            # A negative sentinel (-0 too) clears the per-SBC override; -inf
            # isn't one: it is refused with the other non-finite values.
            value = power_cycle_delay_seconds
            if (
                value != -math.inf
                and not math.isnan(value)
                and math.copysign(1.0, value) < 0
            ):
                params.append(None)
                changes.append("cycle_delay=cleared")
            else:
                from labctl.actuators.runtime import validate_cycle_delay

                # Refuse what a power cycle would refuse: a stored delay is
                # its default and floor, so it would block every cycle.
                validate_cycle_delay(power_cycle_delay_seconds)
                value = float(power_cycle_delay_seconds)
                params.append(value)
                changes.append(f"cycle_delay={value}s")

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE sbcs SET {', '.join(updates)} WHERE id = ?"
            params.append(sbc_id)
            self.db.execute_modify(sql, tuple(params))
            new_name = name if name else sbc.name
            detail = f"Updated SBC {sbc.name}: " + ", ".join(changes)
            self._audit_log("update", "sbc", sbc_id, new_name, detail)
            if status is not None and status != sbc.status:
                # Status history keeps every transition, set by hand too.
                self.log_status(sbc_id, status, "set by hand")

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
        # "/dev/lab/<name>" (normalised) and bare "<name>" (legacy rows),
        # parsed as everywhere else (_lab_link_name).
        name = self._lab_link_name(device_path)
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
            tcp_port: TCP port for ser2net (auto-assigned if None: one
                above the highest port in use, and at least the manager's
                base_tcp_port)
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
            tcp_port = self.next_tcp_port()
            if not 1 <= tcp_port <= 65535:
                # Re-assigning a port that holds the highest TCP port: keep it.
                own = self.db.execute_one(
                    "SELECT tcp_port FROM serial_ports"
                    " WHERE sbc_id = ? AND port_type = ?",
                    (sbc_id, port_type.value),
                )
                if not (own and own["tcp_port"]):
                    raise ValueError(
                        f"No TCP port left to auto-assign (next would be "
                        f"{tcp_port}); pass one explicitly"
                    )
                tcp_port = own["tcp_port"]
        else:
            # A whole number, or one written as text ("4000" from JSON or
            # YAML); not a bool (YAML "yes") or a float.
            if isinstance(tcp_port, str) and tcp_port.strip().isdigit():
                tcp_port = int(tcp_port)
            if isinstance(tcp_port, bool) or not isinstance(tcp_port, int):
                raise ValueError(f"TCP port {tcp_port!r} is not a whole number")
            if not 1 <= tcp_port <= 65535:
                raise ValueError(f"TCP port {tcp_port} is out of range (1-65535)")
            # ser2net serves one port per TCP port, and a board given another
            # board's TCP port would reach that board's console past its
            # claim (D013).
            taken = self.db.execute_one(
                "SELECT 1 FROM serial_ports WHERE tcp_port = ?"
                " AND NOT (sbc_id = ? AND port_type = ?)",
                (tcp_port, sbc_id, port_type.value),
            )
            if taken:
                raise ValueError(f"TCP port {tcp_port} is already in use")

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
            """,  # noqa: E501
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

    def get_serial_port(self, sbc_id: int, port_type: PortType) -> Optional[SerialPort]:
        """The SBC's port of this type, or None."""
        row = self.db.execute_one(
            "SELECT * FROM serial_ports WHERE sbc_id = ? AND port_type = ?",
            (sbc_id, port_type.value),
        )
        if not row:
            return None
        port = SerialPort.from_row(row)
        self._load_serial_device(port)
        return port

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

    def next_tcp_port(self) -> int:
        """Next TCP port to auto-assign: one above the highest in use, and
        never below ``base_tcp_port``."""
        base_port = self.base_tcp_port
        row = self.db.execute_one("SELECT MAX(tcp_port) as max_port FROM serial_ports")
        if row and row["max_port"]:
            return max(row["max_port"] + 1, base_port)
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
        if count:
            self._audit_removal(
                "network_address", sbc_id, f"{address_type.value} address"
            )
        return count > 0

    def _audit_removal(self, entity_type: str, sbc_id: int, what: str) -> None:
        """Record that ``what`` was removed from an SBC (#57)."""
        sbc = self.get_sbc(sbc_id)
        name = sbc.name if sbc else str(sbc_id)
        self._audit_log(
            "remove", entity_type, None, name, f"Removed {what} from {name}"
        )

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
        if count:
            self._audit_removal("power_plug", sbc_id, "power plug")
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

    def sbcs_sharing_hardware(
        self,
        *,
        plug: Optional[tuple[str, str, int]] = None,
        sdwire_device_id: Optional[int] = None,
        serial_device_path: Optional[str] = None,
        serial_device_id: Optional[int] = None,
        tcp_port: Optional[int] = None,
    ) -> list[str]:
        """Names of SBCs already wired to this plug outlet (type, address,
        index), SDWire device or serial device path. Nothing in the schema
        stops two boards sharing one, so a claim check on a new assignment
        has to cover the boards that have it already (D013)."""
        names: set[str] = set()
        if plug is not None:
            wanted = outlet_key(*plug)
            # SQL narrows to the plug type only: SQLite's trim()/lower()
            # don't match str.strip()/lower() (tabs, newlines, non-ASCII),
            # so the address is compared through outlet_key in Python.
            rows = self.db.execute(
                "SELECT s.name, p.plug_type, p.address, p.plug_index"
                " FROM power_plugs p JOIN sbcs s ON s.id = p.sbc_id"
                " WHERE p.plug_type = ?",
                (wanted[0],),
            )
            names.update(
                r["name"]
                for r in rows
                if outlets_overlap(
                    wanted, outlet_key(r["plug_type"], r["address"], r["plug_index"])
                )
            )
        if sdwire_device_id is not None:
            rows = self.db.execute(
                "SELECT s.name FROM sdwire_assignments a"
                " JOIN sbcs s ON s.id = a.sbc_id WHERE a.sdwire_device_id = ?",
                (sdwire_device_id,),
            )
            names.update(r["name"] for r in rows)
        if serial_device_path is not None or serial_device_id is not None:
            wanted = self._serial_identity(serial_device_path, serial_device_id)
            rows = self.db.execute(
                "SELECT s.name, p.device_path, p.serial_device_id"
                " FROM serial_ports p JOIN sbcs s ON s.id = p.sbc_id"
            )
            names.update(
                r["name"]
                for r in rows
                if wanted
                & self._serial_identity(r["device_path"], r["serial_device_id"])
            )
        if tcp_port is not None:
            # One ser2net TCP port is one console, whatever the device path
            # says (rows saved before assignment refused duplicates).
            rows = self.db.execute(
                "SELECT s.name FROM serial_ports p JOIN sbcs s ON s.id = p.sbc_id"
                " WHERE p.tcp_port = ?",
                (tcp_port,),
            )
            names.update(r["name"] for r in rows)
        return sorted(names)

    def sbcs_sharing_hardware_with(self, sbc_name: str) -> list[str]:
        """Other SBCs wired to any of ``sbc_name``'s plug outlet, SDWire,
        serial devices or relay channels: acting on one acts on the other,
        so a claim on either covers both (D013)."""
        sbc = self.get_sbc_by_name(sbc_name)
        if sbc is None:
            return []
        names: set[str] = set()
        if sbc.power_plug is not None:
            plug = sbc.power_plug
            names.update(
                self.sbcs_sharing_hardware(
                    plug=(plug.plug_type.value, plug.address, plug.plug_index)
                )
            )
        row = self.db.execute_one(
            "SELECT sdwire_device_id FROM sdwire_assignments WHERE sbc_id = ?",
            (sbc.id,),
        )
        if row:
            names.update(
                self.sbcs_sharing_hardware(sdwire_device_id=row["sdwire_device_id"])
            )
        for port in sbc.serial_ports:
            names.update(
                self.sbcs_sharing_hardware(
                    serial_device_path=port.device_path,
                    serial_device_id=port.serial_device_id,
                    tcp_port=port.tcp_port,
                )
            )
        channel_ids = {b.actuator_channel_id for b in self.list_bindings(sbc_id=sbc.id)}
        if channel_ids:
            for b in self.list_bindings(
                channel_ids=self.channels_on_same_device(channel_ids)
            ):
                names.add(b.sbc_name)
        names.discard(sbc_name)
        names.discard(None)
        return sorted(names)

    def sbcs_using_serial_links(self, link_names: Optional[Iterable[str]]) -> list[str]:
        """Names of SBCs with a serial port that reaches its adapter through
        one of these ``/dev/lab/<name>`` links (None: any link): assigned by
        that path (or a bare legacy ``<name>``), or linked to the registered
        adapter of that name. A port assigned by another path
        (``/dev/ttyUSB0``, ``/dev/serial/by-id/...``) doesn't use a link."""
        wanted = None if link_names is None else set(link_names)
        if wanted is not None and not wanted:
            return []
        rows = self.db.execute(
            "SELECT s.name, p.device_path, d.name AS device_name"
            " FROM serial_ports p JOIN sbcs s ON s.id = p.sbc_id"
            " LEFT JOIN serial_devices d ON d.id = p.serial_device_id"
        )
        names: set[str] = set()
        for r in rows:
            link = self._lab_link_name(r["device_path"])
            used = {n for n in (link, r["device_name"]) if n}
            if used and (wanted is None or used & wanted):
                names.add(r["name"])
        return sorted(names)

    @staticmethod
    def _lab_link_name(device_path: Optional[str]) -> Optional[str]:
        """The ``<name>`` of a ``/dev/lab/<name>`` path (normalised first,
        so ``/dev//lab/./x`` and ``//dev/lab/x`` count), a bare legacy
        ``<name>``, or None for any other path."""
        if not device_path:
            return None
        if "/" not in device_path:
            return device_path
        if not device_path.startswith("/"):
            return None
        path = "/" + os.path.normpath(device_path).lstrip("/")
        prefix = "/dev/lab/"
        if path.startswith(prefix) and "/" not in path[len(prefix) :]:
            return path[len(prefix) :] or None
        return None

    def _serial_identity(
        self, device_path: Optional[str], serial_device_id: Optional[int]
    ) -> set:
        """Keys naming a serial adapter: its registered device (given, or
        from a ``/dev/lab/<name>`` path) and its path with symlinks resolved
        (``/dev/lab/port-1``, ``/dev/ttyUSB0`` and ``/dev/serial/by-id/...``
        for one adapter all resolve to the same node)."""
        keys: set = set()
        if serial_device_id is None and device_path:
            serial_device_id = self._resolve_serial_device_id(device_path)
        if serial_device_id is not None:
            keys.add(("device", serial_device_id))
        if device_path:
            keys.add(("path", os.path.realpath(device_path)))
        return keys

    def channels_on_same_device(self, channel_ids: Iterable[int]) -> set[int]:
        """``channel_ids`` plus the same-index channels of any other
        actuator record for the same relay board (nothing stops two records
        naming one). Records match by device path after resolving symlinks
        (``/dev/lab/relay-a`` and the ``/dev/ttyUSB0`` it points to), or by
        USB serial number with VID/PID. Records identified only by VID/PID can't be told
        apart from other boards of the same model and aren't matched."""
        ids = set(channel_ids)
        if not ids:
            return ids
        by_channel = {
            c.id: (a, c.channel_index)
            for a in self.list_actuators()
            for c in a.channels
        }

        def identity(actuator) -> set:
            keys = set()
            if actuator.device_path:
                keys.add(("path", os.path.realpath(actuator.device_path)))
            # Cheap USB-serial chips often share placeholder serial
            # numbers, so a serial number only identifies a board together
            # with its VID/PID.
            if actuator.serial_no and actuator.vid and actuator.pid:
                keys.add(
                    (
                        "serial",
                        _norm_usb_id(actuator.vid),
                        _norm_usb_id(actuator.pid),
                        actuator.serial_no.strip(),
                    )
                )
            return keys

        # Repeat until stable: a record may share its path with one record
        # and its serial number with another.
        while True:
            wanted = {
                (key, by_channel[cid][1])
                for cid in ids
                if cid in by_channel
                for key in identity(by_channel[cid][0])
            }
            found = {
                cid
                for cid, (actuator, index) in by_channel.items()
                if any((key, index) in wanted for key in identity(actuator))
            }
            if found <= ids:
                return ids
            ids |= found

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
               ON CONFLICT (sbc_id) DO UPDATE SET sdwire_device_id = excluded.sdwire_device_id""",  # noqa: E501
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
        if count:
            self._audit_removal("sdwire", sbc_id, "SDWire assignment")
        return count > 0

    # --- Status Log ---

    def update_power_observation(
        self,
        sbc_id: int,
        power_state: Optional[str],
    ) -> None:
        """Record the daemon's most recent power observation for an SBC.

        Stamps `last_power_at` with the current time even when the
        observed state hasn't changed, so `status --fast` can report
        freshness. A `power_state` of None records "unknown" so callers
        can distinguish "no plug / never observed" (NULL in the column)
        from "observed but indeterminate".
        """
        normalized = (power_state or "unknown").lower()
        if normalized not in {"on", "off", "unknown"}:
            normalized = "unknown"
        self.db.execute_modify(
            """
            UPDATE sbcs
            SET last_power_state = ?,
                last_power_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (normalized, sbc_id),
        )

    def record_status(
        self, sbc_id: int, status: Status, details: Optional[str] = None
    ) -> None:
        """Store a status the health checks observed, and its history row.

        Not an activity event: a status is an observation, not an action
        (#57), and status_log keeps every transition. Setting a status by
        hand goes through update_sbc, which records both.
        """
        if self.db.execute_modify(
            "UPDATE sbcs SET status = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (status.value, sbc_id),
        ):  # not for a board deleted since it was checked
            self.log_status(sbc_id, status, details)

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

    # Rows one prune transaction deletes at most: the first prune after an
    # upgrade may have years of history, and each batch holds the write
    # lock only briefly.
    PRUNE_BATCH = 5000

    def cleanup_old_status_logs(
        self, retention_days: int, *, batch_size: Optional[int] = None
    ) -> int:
        """
        Delete status log entries older than retention period.

        Rows that readers still need are kept whatever their age: for each
        SBC, the newest row of each status (``get_uptime``'s "last online"
        and "last offline", so a board stable for longer than the retention
        keeps its uptime) and the newest row before the cutoff (the status
        in effect when the kept history starts, which the 24-hour uptime
        figure starts from).

        Rows are deleted in transactions of at most ``batch_size``
        (default PRUNE_BATCH), the keep rules applied afresh each time.

        Args:
            retention_days: Number of days to retain status logs
            batch_size: Rows per delete transaction

        Returns:
            Number of deleted entries
        """
        cutoff = f"-{int(retention_days)} days"
        batch = batch_size or self.PRUNE_BATCH
        # The monitor calls this hourly, and the rows always kept are past
        # the cutoff, so "anything past the cutoff?" is nearly always yes.
        # Instead, remember the table's state after the last prune with
        # this retention: the newest row id, the number of rows past the
        # cutoff (an index count) and the total. If none changed, nothing
        # can have become deletable: no row has aged past the cutoff, no
        # new row has replaced a kept one as its board's newest for its
        # status, and no rows were deleted meanwhile (e.g. with a removed
        # board) that ageing could have made up for. The DELETE is skipped.
        state = self._status_log_state(cutoff)
        if state[1] == 0 or (retention_days, state) == self._prune_state:
            self._prune_state = (retention_days, state)
            return 0
        deleted = 0
        while True:
            # A plain DELETE (not WITH ... DELETE): sqlite3 reports
            # rowcount only for statements that start with DELETE.
            count = self.db.execute_modify(
                """
                DELETE FROM status_log WHERE id IN (
                  SELECT id FROM status_log
                  WHERE logged_at < datetime('now', ?)
                    AND id NOT IN (
                      SELECT id FROM (
                          SELECT id, ROW_NUMBER() OVER (
                              PARTITION BY sbc_id, status
                              ORDER BY logged_at DESC, id DESC
                          ) AS rn
                          FROM status_log
                      ) WHERE rn = 1
                      UNION
                      SELECT id FROM (
                          SELECT id, ROW_NUMBER() OVER (
                              PARTITION BY sbc_id ORDER BY logged_at DESC, id DESC
                          ) AS rn
                          FROM status_log
                          WHERE logged_at < datetime('now', ?)
                      ) WHERE rn = 1
                    )
                  ORDER BY logged_at, id
                  LIMIT ?
                )
                """,
                (cutoff, cutoff, batch),
            )
            deleted += count
            if count < batch:
                break
        self._prune_state = (retention_days, self._status_log_state(cutoff))
        return deleted

    def _status_log_state(self, cutoff: str) -> tuple:
        """(newest id, rows older than ``cutoff``, all rows)."""
        row = self.db.execute_one(
            "SELECT (SELECT MAX(id) FROM status_log) AS top,"
            " (SELECT COUNT(*) FROM status_log"
            "  WHERE logged_at < datetime('now', ?)) AS old,"
            " (SELECT COUNT(*) FROM status_log) AS total",
            (cutoff,),
        )
        return (row["top"], row["old"], row["total"])

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

        # status_log.logged_at is SQLite's CURRENT_TIMESTAMP: UTC, as
        # "YYYY-MM-DD HH:MM:SS". Compute in the same clock and format.
        def parse(ts: str) -> datetime:
            return datetime.fromisoformat(ts)

        # Get last online transition
        last_online_row = self.db.execute_one(
            """
            SELECT id, logged_at FROM status_log
            WHERE sbc_id = ? AND status = 'online'
            ORDER BY logged_at DESC, id DESC
            LIMIT 1
            """,
            (sbc_id,),
        )

        # Get last offline transition
        last_offline_row = self.db.execute_one(
            """
            SELECT id, logged_at FROM status_log
            WHERE sbc_id = ? AND status IN ('offline', 'error')
            ORDER BY logged_at DESC, id DESC
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

        now = parse(self.db.execute_one("SELECT datetime('now') AS t")["t"])

        # Calculate current uptime if online
        if sbc.status == Status.ONLINE and last_online_row:
            last_online = parse(last_online_row["logged_at"])
            # Check if we went offline after going online
            if last_offline_row:
                last_offline = parse(last_offline_row["logged_at"])
                # Same second (CURRENT_TIMESTAMP's resolution): the row
                # logged later, i.e. with the higher id, is the newer one.
                offline_is_newer = (last_offline, last_offline_row["id"]) > (
                    last_online,
                    last_online_row["id"],
                )
                if offline_is_newer:
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

        # Calculate 24h uptime percentage, starting from the status in
        # effect 24 hours ago (the newest row before then), so a board that
        # has been stable for longer counts as up or down the whole time.
        day_ago = now - timedelta(hours=24)
        day_ago_text = day_ago.strftime("%Y-%m-%d %H:%M:%S")
        before = self.db.execute_one(
            """
            SELECT status FROM status_log
            WHERE sbc_id = ? AND logged_at < ?
            ORDER BY logged_at DESC, id DESC
            LIMIT 1
            """,
            (sbc_id, day_ago_text),
        )
        rows = self.db.execute(
            """
            SELECT status, logged_at FROM status_log
            WHERE sbc_id = ? AND logged_at >= ?
            ORDER BY logged_at ASC, id ASC
            """,
            (sbc_id, day_ago_text),
        )

        if rows or before:
            online_seconds = 0
            prev_status = before["status"] if before else None
            prev_time = day_ago

            for row in rows:
                status = row["status"]
                time_str = row["logged_at"]
                log_time = parse(time_str)

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

    # Microsecond-precision UTC text (timeutil.to_db): short expiry
    # comparisons don't collapse to a second, and the layout compares
    # lexically in the same order as the datetimes.
    @classmethod
    def _fmt_ts(cls, dt: datetime) -> str:
        """``dt`` as stored: UTC (#63)."""
        return timeutil.to_db(dt)

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

    def sweep_claims(
        self,
        grace_seconds: int,
        prune_after_days: int,
        activity_retention_days: int = 30,
    ) -> dict:
        """One claim sweep, shared by ``labctl claims expire`` and the MCP
        server's sweep thread: release expired and dead-session claims,
        prune released claims and old activity. Returns the counts."""
        return {
            "expired": self.expire_stale_claims(grace_seconds=grace_seconds),
            "dead": self.release_dead_sessions(grace_seconds=grace_seconds),
            "pruned": self.prune_released_claims(older_than_days=prune_after_days),
            "activity_pruned": self.prune_activity_events(
                older_than_days=activity_retention_days
            ),
        }

    def expire_stale_claims(self, grace_seconds: int = 60) -> int:
        """Mark claims past their deadline (+ grace) as released.

        Returns the number of claims released by this sweep. Safe to call
        repeatedly; only unreleased rows with an elapsed deadline are touched.
        """
        cutoff = self._fmt_ts(timeutil.now() - timedelta(seconds=grace_seconds))
        with self.db.connect() as conn:
            self._begin_write(conn)
            expired = self._release_expired(conn, cutoff)
        self._record_expired(expired)
        return len(expired)

    @staticmethod
    def _begin_write(conn) -> None:
        """Start ``conn``'s transaction with the write lock (BEGIN
        IMMEDIATE), so what it reads stays true until it commits. Call it
        first: a transaction already open (Python's implicit deferred
        BEGIN) would not hold the lock, so that is refused."""
        if conn.in_transaction:
            raise RuntimeError("_begin_write must start the transaction")
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _release_expired(conn, cutoff: str, holder: Optional[tuple] = None) -> list:
        """Release unreleased claims whose deadline is before ``cutoff``,
        in a transaction begun with _begin_write; return them (id, sbc_id,
        agent_name, sbc_name) for _record_expired. ``holder`` =
        (sbc_id, session_id, owner) limits it to that holder's claim on
        that SBC. Under the write lock nothing changes between the SELECT
        and the UPDATE, so each expiry is released, and recorded, once.
        """
        if not conn.in_transaction:
            raise RuntimeError("_release_expired needs _begin_write first")
        where = "c.released_at IS NULL AND c.expires_at < ?"
        params: list = [cutoff]
        if holder is not None:
            sbc_id, session_id, owner = holder
            where += (
                " AND c.sbc_id = ?"
                " AND (c.session_id = ? OR (c.owner IS NOT NULL AND c.owner = ?))"
            )
            params += [sbc_id, session_id, owner]
        expired = conn.execute(
            "SELECT c.id, c.sbc_id, c.agent_name, s.name AS sbc_name"
            f" FROM claims c JOIN sbcs s ON s.id = c.sbc_id WHERE {where}",
            params,
        ).fetchall()
        if expired:
            ids = [row["id"] for row in expired]
            conn.execute(
                f"""
                UPDATE claims
                SET released_at = CURRENT_TIMESTAMP,
                    release_reason = ?,
                    released_by = 'system'
                WHERE released_at IS NULL AND id IN ({", ".join("?" * len(ids))})
                """,
                (ReleaseReason.EXPIRED.value, *ids),
            )
        return expired

    def _record_expired(self, expired: list, message: Optional[str] = None) -> None:
        """One claim_expired event per released claim. The claim ran out:
        whoever's call swept it didn't release it, so it is the system's.

        The claim's ID goes in the details, not audit_log.claim_id: that
        column references claims(id) with no ON DELETE, so filling it
        would stop the claim (and its SBC) from ever being deleted.
        """
        for row in expired:
            with audit.activity_context("system", "internal"):
                audit.emit(
                    self.db,
                    action="claim_expired",
                    entity_type="sbc",
                    entity_id=row["sbc_id"],
                    entity_name=row["sbc_name"],
                    details={
                        "message": message or f"Claim by {row['agent_name']} expired",
                        "claim": row["id"],
                    },
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

    @staticmethod
    def _process_start_epoch(pid: int) -> Optional[float]:
        """Wall-clock start time of ``pid`` from /proc, or None if unknown.

        Linux only: start = boot time (``btime`` in /proc/stat) + the
        process's ``starttime`` (field 22 of /proc/<pid>/stat, in clock
        ticks). Returns None on other platforms or any read/parse failure.
        """
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            # comm (field 2) is parenthesised and may contain spaces or ")",
            # so split after the last ")": the rest starts at field 3.
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            with open("/proc/stat") as f:
                btime = next(
                    int(line.split()[1]) for line in f if line.startswith("btime ")
                )
            return btime + start_ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            return None

    # Slack for comparing a process start time with a session epoch: both are
    # derived from the wall clock (btime shifts with clock steps) and rounded.
    _SESSION_START_SLACK_SECONDS = 2.0

    def _is_session_alive(self, pid: int, epoch: Optional[int]) -> bool:
        """True if ``pid`` is still the process that created the session.

        An mcp-stdio session ID is ``mcp-stdio:<pid>-<epoch>``, where the
        server records ``epoch`` shortly *after* it starts. So the original
        process started at or before ``epoch``, while a process that reuses
        the PID started after the original exited. When the start time
        can't be read, fall back to the PID check alone.
        """
        if not self._is_pid_alive(pid):
            return False
        if epoch is None:
            return True
        started = self._process_start_epoch(pid)
        if started is None:
            return True
        return started <= epoch + self._SESSION_START_SLACK_SECONDS

    def release_dead_sessions(self, grace_seconds: int = 60) -> int:
        """Release claims whose MCP server process has exited.

        Parses ``mcp-stdio:<pid>-<epoch>`` and ``mcp-http:<pid>-<epoch>``
        session IDs (HTTP ones may end in ``:<username>``). If the session's
        process is gone (dead PID, or the PID now belongs to a newer
        process — see ``_is_session_alive``) **and** the claim's deadline +
        grace has passed, the claim is released as ``session-lost``. Claims
        with other ``session_kind`` values (cli, web) are skipped.
        """
        rows = self.db.execute("""
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.released_at IS NULL
              AND c.session_kind IN ('mcp-stdio', 'mcp-http')
            """)
        released = 0
        cutoff = timeutil.now() - timedelta(seconds=grace_seconds)
        for row in rows:
            claim = Claim.from_row(row)
            # Parse "mcp-<transport>:<pid>-<epoch>" (epoch optional for old IDs)
            try:
                payload = claim.session_id.split(":", 1)[1]
                pid_part, _, epoch_part = payload.partition("-")
                pid = int(pid_part)
            except (IndexError, ValueError):
                continue
            try:
                # Authenticated MCP HTTP appends ":<username>".
                epoch = int(epoch_part.split(":", 1)[0])
            except ValueError:
                epoch = None

            if self._is_session_alive(pid, epoch):
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
        cutoff = self._fmt_ts(timeutil.now() - timedelta(days=older_than_days))
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
        rows = self.db.execute("""
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
            """)  # noqa: E501
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
        owner: Optional[str] = None,
    ) -> Claim:
        """Acquire an exclusive claim on an SBC.

        ``owner`` is the authenticated labctl username, if any (see
        :class:`ClaimActor`).

        Runs an expire-stale sweep inside the acquisition transaction so
        the partial unique index can't block a legitimate caller behind a
        claim whose deadline has already passed.

        Refuses (via ChannelBusyError) if any actuator channel referenced
        by this SBC's bindings is currently held by an in-flight verb.
        Snapshot check only; ownership across the claim's lifetime is
        enforced by the DB-backed claim row.
        """
        sbc_id = self._require_sbc_id(sbc_name)

        # Phase 6 gate: no actuator-channel verb in flight on bound channels.
        # Imported lazily to avoid cycles and to keep the manager core
        # decoupled from the actuators package at import time.
        from labctl.actuators.runtime import check_no_channel_busy_for_sbc

        check_no_channel_busy_for_sbc(self, sbc_id)
        context_json = json.dumps(context) if context else None
        cutoff = self._fmt_ts(timeutil.now() - timedelta(seconds=grace_seconds))

        with self.db.connect() as conn:
            # Release expired claims (on any SBC) within the same
            # transaction so the UNIQUE index reflects current reality.
            self._begin_write(conn)
            swept = self._release_expired(conn, cutoff)
            # The grace period protects an expired claim from others, not
            # from its own holder: re-claiming replaces it straight away.
            replaced = self._release_expired(
                conn,
                self._fmt_ts(timeutil.now()),
                holder=(sbc_id, session_id, owner),
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

            now = timeutil.now()
            expires_at = now + timedelta(seconds=duration_seconds)
            cursor = conn.execute(
                """
                INSERT INTO claims (
                    sbc_id, agent_name, session_id, session_kind, owner,
                    reason, context_json, acquired_at, duration_seconds,
                    last_activity, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sbc_id,
                    agent_name,
                    session_id,
                    session_kind,
                    owner or None,
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

        self._record_expired(swept)
        self._record_expired(
            replaced, "Expired claim replaced by its holder's new claim"
        )
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
        claim = self.get_open_claim(sbc_name)
        # If materialized deadline passed, treat as free — the next sweep
        # will commit the release. Don't return a zombie as active.
        if claim is None or not claim.is_active:
            return None
        self._load_pending_requests(claim)
        return claim

    def get_open_claim(self, sbc_name: str) -> Optional[Claim]:
        """The unreleased claim on an SBC, including one past its deadline
        that hasn't been swept yet, or None."""
        sbc_id = self._require_sbc_id(sbc_name)
        row = self.db.execute_one(
            """
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.sbc_id = ? AND c.released_at IS NULL
            """,
            (sbc_id,),
        )
        return Claim.from_row(row) if row else None

    def list_active_claims(self) -> list[Claim]:
        """All currently active claims across the lab."""
        rows = self.db.execute("""
            SELECT c.*, s.name AS sbc_name
            FROM claims c JOIN sbcs s ON s.id = c.sbc_id
            WHERE c.released_at IS NULL
            ORDER BY c.acquired_at ASC
            """)
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

    def release_claim(
        self,
        sbc_name: str,
        session_id: str,
        owner: Optional[str] = None,
        released_by: Optional[str] = None,
    ) -> Claim:
        """Release the active claim held by ``session_id`` (or by the
        authenticated user ``owner``, from any session) on an SBC.
        ``released_by`` names who released it (default: the claim's
        agent name)."""
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        if not ClaimActor(session_id, owner).holds(claim):
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
            (ReleaseReason.RELEASED.value, released_by or claim.agent_name, claim.id),
        )
        self._audit_log(
            "release",
            "sbc",
            claim.sbc_id,
            sbc_name,
            f"Released by {released_by or claim.agent_name}",
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
        owner: Optional[str] = None,
    ) -> Claim:
        """Extend an active claim's deadline (held by ``session_id`` or
        by the authenticated user ``owner``).

        If ``duration_seconds`` is omitted the previous duration is reused.
        ``last_activity`` and ``expires_at`` are both advanced to "now" and
        "now + duration_seconds" respectively.
        """
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        if not ClaimActor(session_id, owner).holds(claim):
            raise NotClaimantError(
                f"Claim on '{sbc_name}' is held by a different session"
            )
        new_duration = (
            duration_seconds if duration_seconds is not None else claim.duration_seconds
        )
        now = timeutil.now()
        new_expiry = now + timedelta(seconds=new_duration)
        updated = self.db.execute_modify(
            """
            UPDATE claims
            SET duration_seconds = ?,
                last_activity = ?,
                expires_at = ?,
                renewal_count = renewal_count + 1
            WHERE id = ? AND released_at IS NULL AND expires_at >= ?
            """,
            (
                new_duration,
                self._fmt_ts(now),
                self._fmt_ts(new_expiry),
                claim.id,
                self._fmt_ts(now),
            ),
        )
        if not updated:  # released or expired meanwhile
            raise ClaimNotFoundError(f"No active claim on SBC '{sbc_name}'")
        audit.emit(
            self.db,
            action="claim_renew",
            entity_type="sbc",
            entity_id=claim.sbc_id,
            entity_name=sbc_name,
            details={
                "message": f"Claim by {claim.agent_name} renewed for {new_duration}s",
                "claim": claim.id,
            },
        )
        return self._get_claim_by_id(claim.id)

    def heartbeat_held_claim(
        self, sbc_name: str, actor: ClaimActor, claim_id: Optional[int] = None
    ) -> bool:
        """Heartbeat ``actor``'s active claim on ``sbc_name``; True if it
        still holds one (and it was heartbeated), False otherwise.

        One conditional UPDATE: the claim must be unreleased, not past its
        deadline, and held by ``actor`` (:meth:`ClaimActor.holds`), so a
        claim released or taken over meanwhile is never revived. The new
        deadline is computed from the row's own duration, so a renew to
        another duration is respected. With ``claim_id``, only that claim
        counts (not a later one by the same holder). Unknown SBCs raise
        UnknownSBCError.
        """
        sbc_id = self._require_sbc_id(sbc_name)
        now = self._fmt_ts(timeutil.now())
        changed = self.db.execute_modify(
            """
            UPDATE claims
            SET last_activity = ?,
                expires_at = strftime(
                    '%Y-%m-%d %H:%M:%f', ?, '+' || duration_seconds || ' seconds'
                ) || '000'
            WHERE sbc_id = ? AND released_at IS NULL AND expires_at >= ?
              AND (session_id = ? OR (COALESCE(?, '') != '' AND owner = ?))
              AND (? IS NULL OR id = ?)
            """,
            (
                now,
                now,
                sbc_id,
                now,
                actor.session_id,
                actor.owner,
                actor.owner,
                claim_id,
                claim_id,
            ),
        )
        return changed > 0

    def touch_claim(self, claim: Claim) -> bool:
        """Heartbeat an already-loaded active claim. Returns False when no
        row changed (released or past its deadline meanwhile)."""
        now = timeutil.now()
        new_expiry = now + timedelta(seconds=claim.duration_seconds)
        changed = self.db.execute_modify(
            """
            UPDATE claims
            SET last_activity = ?, expires_at = ?
            WHERE id = ? AND released_at IS NULL AND expires_at >= ?
            """,
            (self._fmt_ts(now), self._fmt_ts(new_expiry), claim.id, self._fmt_ts(now)),
        )
        return changed > 0

    def check_claim(
        self,
        sbc_name: str,
        actor: ClaimActor,
        *,
        override: bool = False,
        require_claim: bool = False,
        include_shared: bool = True,
        heartbeat: bool = True,
    ) -> Optional[Claim]:
        """Gate an operation that changes ``sbc_name`` on its claim (D013).

        Returns the active claim (or None) when ``actor`` may proceed:
        no claim (unless ``require_claim``), the actor's own claim (which
        is heartbeated), or an operator's explicit ``override`` (recorded
        in the activity log). Raises :class:`ClaimConflict` when someone
        else holds the claim, :class:`NotOperatorError` when an agent asks
        to override, and :class:`ClaimRequiredError` when ``require_claim``
        is set and nobody holds it (an operator's ``override`` also covers
        that case, and is logged). Unknown SBCs raise UnknownSBCError.

        With ``include_shared`` (the default), boards that share hardware
        with ``sbc_name`` (see :meth:`sbcs_sharing_hardware_with`) are
        checked too, for someone else's claim only: ``require_claim``
        applies to the named board alone. A conflict there raises
        ClaimConflict carrying that board's claim. Only the named board's
        claim is heartbeated, and not at all with ``heartbeat=False``.
        """
        # Heartbeat only once every check has passed (a refused call
        # mustn't extend the claim), and only the named board's claim
        # (using A mustn't keep the caller's claim on a sharing B alive).
        claim = self._check_one_claim(sbc_name, actor, override, require_claim)
        if include_shared:
            for other in self.sbcs_sharing_hardware_with(sbc_name):
                try:
                    self._check_one_claim(other, actor, override, False)
                except UnknownSBCError:
                    continue  # deleted meanwhile: nothing left to protect
        if heartbeat and claim is not None and actor.holds(claim):
            self.touch_claim(claim)
        return claim

    def record_claim_refusal(
        self,
        sbc_name: str,
        reason: str,
        *,
        claim: Optional[Claim] = None,
        attempted: Optional[str] = None,
    ) -> None:
        """Record that a call on ``sbc_name`` was refused over a claim
        (#60): ``claim_refused``, result ``forbidden``. ``reason`` is
        ``claimed`` (someone else's ``claim``, possibly on a board sharing
        its hardware), ``claim_required``, ``not_operator`` (an agent's
        force-release) or ``not_claimant`` (releasing or renewing someone
        else's claim);
        ``attempted`` names the tool, command or route. The front ends call
        this where they refuse, never for checks that only look."""
        details: dict = {"reason": reason, "attempted": attempted}
        if claim is not None:
            details.update(holder=claim.agent_name, claim=claim.id)
            if claim.sbc_name and claim.sbc_name != sbc_name:
                details["held_board"] = claim.sbc_name
        try:  # like emit, never let recording break the refusal itself
            row = self.db.execute_one("SELECT id FROM sbcs WHERE name = ?", (sbc_name,))
        except Exception:  # noqa: BLE001
            logger.warning("claim refusal on %s: board lookup failed", sbc_name)
            row = None
        audit.emit(
            self.db,
            action="claim_refused",
            entity_type="sbc",
            entity_id=row["id"] if row else None,
            entity_name=sbc_name,
            result="forbidden",
            details={k: v for k, v in details.items() if v is not None},
        )

    def record_override_refusal(self, sbc_name: str, attempted: Optional[str]) -> None:
        """Record a non-operator's refused force-release of ``sbc_name``'s
        claim, when there is one to release (a typo or an unclaimed board
        is not a claim refusal)."""
        self._record_refusal_of_held(sbc_name, "not_operator", attempted)

    def record_not_claimant(self, sbc_name: str, attempted: Optional[str]) -> None:
        """Record a refused release or renewal of someone else's claim on
        ``sbc_name`` (``not_claimant``)."""
        self._record_refusal_of_held(sbc_name, "not_claimant", attempted)

    def _record_refusal_of_held(
        self, sbc_name: str, reason: str, attempted: Optional[str]
    ) -> None:
        try:
            claim = self.get_active_claim(sbc_name)
        except Exception:  # noqa: BLE001 - unknown board, or lookup failed
            claim = None
        if claim is not None:
            self.record_claim_refusal(
                sbc_name, reason, claim=claim, attempted=attempted
            )

    def check_claims_of(
        self, sbc_names: Iterable[str], actor: ClaimActor, *, override: bool = False
    ) -> None:
        """:meth:`check_claim` for boards a call reaches only through
        shared hardware (wiring a claimed board's plug to another board,
        driving a relay bound to it): someone else's claim refuses, but
        ``require_claim`` doesn't apply, nothing is heartbeated and their
        own sharers aren't followed. Boards deleted meanwhile are skipped."""
        for name in sorted({n for n in sbc_names if n}):
            try:
                self.check_claim(
                    name,
                    actor,
                    override=override,
                    include_shared=False,
                    heartbeat=False,
                )
            except UnknownSBCError:
                continue

    def sbcs_bound_to_channels(
        self, channel_ids: Iterable[int], *, same_device: bool = True
    ) -> list[str]:
        """SBCs bound to one of these actuator channels: a relay wired to a
        board's reset or power is part of that board (D013).
        ``same_device`` also covers other actuator records for the same
        relay board (needed when driving a channel, not when deleting a
        record)."""
        channel_ids = set(channel_ids)
        if not channel_ids:
            return []
        if same_device:
            channel_ids = self.channels_on_same_device(channel_ids)
        names = {b.sbc_name for b in self.list_bindings(channel_ids=channel_ids)}
        names.discard(None)
        return sorted(names)

    def new_claim_conflict(
        self, sbc_name: str, actor: ClaimActor, grace_seconds: int
    ) -> Optional[Claim]:
        """The claim that stops ``actor`` claiming ``sbc_name``, or None.

        First the board's own open claim if someone else holds it, while
        active or inside its grace period (only its holder may re-claim it
        then; past the grace :meth:`claim_sbc` sweeps it). Then another
        user's active claim on a board sharing its hardware: the new claim
        would let ``actor`` drive that board. The actor's own claim is not
        returned (callers renew it). Unknown SBCs raise UnknownSBCError.
        """
        own = self.get_open_claim(sbc_name)
        if own is not None and not actor.holds(own):
            cutoff = timeutil.now() - timedelta(seconds=grace_seconds)
            # Same test as claim_sbc's sweep (expires_at < now - grace).
            if own.expires_at is not None and own.expires_at >= cutoff:
                return own
        for other in self.sbcs_sharing_hardware_with(sbc_name):
            try:
                claim = self.get_active_claim(other)
            except UnknownSBCError:
                continue
            if claim is not None and not actor.holds(claim):
                return claim
        return None

    def _check_one_claim(
        self,
        sbc_name: str,
        actor: ClaimActor,
        override: bool,
        require_claim: bool,
    ) -> Optional[Claim]:
        """One board's part of check_claim; never heartbeats (check_claim
        does, once everything has passed)."""
        claim = self.get_active_claim(sbc_name)
        if claim is None:
            if not require_claim:
                return None
            if not override:
                raise ClaimRequiredError(sbc_name)
            if not actor.operator:
                raise NotOperatorError(
                    f"Only operators may act on '{sbc_name}' without a claim"
                )
            self._audit_log(
                "claim_override",
                "sbc",
                self._require_sbc_id(sbc_name),
                sbc_name,
                "Acted on an unclaimed board without a claim (operator override)",
            )
            return None
        if actor.holds(claim):
            return claim
        if not override:
            raise ClaimConflict(claim)
        if not actor.operator:
            raise NotOperatorError(
                f"Only operators may override the claim on '{sbc_name}' "
                f"(held by '{claim.agent_name}')",
                claim=claim,
            )
        self._audit_log(
            "claim_override",
            "sbc",
            claim.sbc_id,
            sbc_name,
            f"Acted on a board claimed by {claim.agent_name} (operator override)",
        )
        return claim

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
        audit.emit(
            self.db,
            action="release_request",
            entity_type="sbc",
            entity_id=claim.sbc_id,
            entity_name=sbc_name,
            details={
                "message": f"{requested_by} asked {claim.agent_name} to release: "
                f"{reason}",
                "claim": claim.id,
            },
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

    # --- Actuator Operations ---

    def create_actuator(
        self,
        name: str,
        driver: DriverName,
        *,
        kind: ActuatorKind = ActuatorKind.RELAY,
        device_path: Optional[str] = None,
        vid: Optional[str] = None,
        pid: Optional[str] = None,
        serial_no: Optional[str] = None,
    ) -> Actuator:
        """Register a new actuator."""
        actuator_id = self.db.execute_insert(
            """
            INSERT INTO actuators
              (name, kind, driver, device_path, vid, pid, serial_no)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                kind.value,
                driver.value,
                device_path,
                vid,
                pid,
                serial_no,
            ),
        )
        self._audit_log(
            "create",
            "actuator",
            actuator_id,
            name,
            f"Registered actuator {name!r} (driver={driver.value})",
        )
        result = self.get_actuator(actuator_id)
        if result is None:
            raise RuntimeError(f"Failed to retrieve actuator {actuator_id}")
        return result

    def get_actuator(self, actuator_id: int) -> Optional[Actuator]:
        row = self.db.execute_one(
            "SELECT * FROM actuators WHERE id = ?", (actuator_id,)
        )
        if not row:
            return None
        actuator = Actuator.from_row(row)
        actuator.channels = self.list_actuator_channels(actuator.id)
        return actuator

    def get_actuator_by_name(self, name: str) -> Optional[Actuator]:
        row = self.db.execute_one("SELECT * FROM actuators WHERE name = ?", (name,))
        if not row:
            return None
        actuator = Actuator.from_row(row)
        actuator.channels = self.list_actuator_channels(actuator.id)
        return actuator

    def list_actuators(self) -> list[Actuator]:
        rows = self.db.execute("SELECT * FROM actuators ORDER BY name")
        actuators = [Actuator.from_row(r) for r in rows]
        # Batch-load channels.
        if actuators:
            ids = [a.id for a in actuators]
            placeholders = ",".join("?" for _ in ids)
            ch_rows = self.db.execute(
                f"SELECT * FROM actuator_channels WHERE actuator_id IN ({placeholders})"
                " ORDER BY actuator_id, channel_index",
                tuple(ids),
            )
            by_aid: dict[int, list[ActuatorChannel]] = {}
            for r in ch_rows:
                by_aid.setdefault(r["actuator_id"], []).append(
                    ActuatorChannel.from_row(r)
                )
            for a in actuators:
                a.channels = by_aid.get(a.id, [])
        return actuators

    def delete_actuator(self, actuator_id: int) -> bool:
        actuator = self.get_actuator(actuator_id)
        if not actuator:
            return False
        # Capture channel IDs before the cascade-delete so we can drop
        # their per-channel locks afterwards. Without this the
        # process-local lock dict accumulates stale entries across
        # reprovisioning cycles.
        channel_ids = [ch.id for ch in actuator.channels]
        count = self.db.execute_modify(
            "DELETE FROM actuators WHERE id = ?", (actuator_id,)
        )
        if count > 0:
            self._audit_log(
                "delete",
                "actuator",
                actuator_id,
                actuator.name,
                f"Deleted actuator {actuator.name!r}",
            )
            # Lazy-import to avoid a manager → actuators → manager cycle
            # at module load time.
            try:
                from labctl.actuators.runtime import drop_channel_lock

                for cid in channel_ids:
                    drop_channel_lock(cid)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to drop per-channel locks for actuator %d", actuator_id
                )
            return True
        return False

    def record_actuator_probe(self, actuator_id: int, result: str) -> None:
        """Stamp `last_probe_at` and `last_probe_result` on an actuator."""
        self.db.execute_modify(
            """
            UPDATE actuators
            SET last_probe_at = CURRENT_TIMESTAMP,
                last_probe_result = ?
            WHERE id = ?
            """,
            (result, actuator_id),
        )

    # --- Actuator Channel Operations ---

    def add_actuator_channel(
        self,
        actuator_id: int,
        channel_index: int,
        *,
        label: Optional[str] = None,
        default_state: ChannelState = ChannelState.OPEN,
    ) -> ActuatorChannel:
        channel_id = self.db.execute_insert(
            """
            INSERT INTO actuator_channels
              (actuator_id, channel_index, label, default_state)
            VALUES (?, ?, ?, ?)
            """,
            (actuator_id, channel_index, label, default_state.value),
        )
        actuator = self.get_actuator(actuator_id)
        actuator_name = actuator.name if actuator else None
        self._audit_log(
            "create",
            "actuator_channel",
            channel_id,
            f"{actuator_name}[{channel_index}]" if actuator_name else None,
            f"Added channel {channel_index} to {actuator_name!r}",
        )
        row = self.db.execute_one(
            "SELECT * FROM actuator_channels WHERE id = ?", (channel_id,)
        )
        if not row:
            raise RuntimeError(f"Failed to retrieve channel {channel_id}")
        return ActuatorChannel.from_row(row)

    def list_actuator_channels(self, actuator_id: int) -> list[ActuatorChannel]:
        rows = self.db.execute(
            "SELECT * FROM actuator_channels WHERE actuator_id = ?"
            " ORDER BY channel_index",
            (actuator_id,),
        )
        return [ActuatorChannel.from_row(r) for r in rows]

    def get_actuator_channel(
        self, actuator_id: int, channel_index: int
    ) -> Optional[ActuatorChannel]:
        row = self.db.execute_one(
            "SELECT * FROM actuator_channels"
            " WHERE actuator_id = ? AND channel_index = ?",
            (actuator_id, channel_index),
        )
        return ActuatorChannel.from_row(row) if row else None

    def update_channel_state(
        self,
        channel_id: int,
        last_state: ChannelState,
        *,
        bump_cycle_count: bool = True,
    ) -> None:
        """Stamp last_state, last_changed_at, and increment cycle_count.

        Called by the actuation layer after a successful write. The
        cycle bump is **suppressed when the new state matches the
        currently-cached state** — mechanical relays only wear on
        actual transitions, so a redundant write (e.g. enter_recovery
        re-applying an already-engaged strap via apply_pre_power) must
        not inflate the wear metric.
        """
        if bump_cycle_count:
            # Only bump when last_state actually changes. SQLite's
            # CASE expression keeps this atomic in one statement so
            # there's no read-then-write race.
            #   - last_state IS NULL  → first ever drive of this channel,
            #     count it as one transition.
            #   - last_state != new   → real transition, bump.
            #   - else                → idempotent re-drive (e.g. pre_power
            #     re-asserting an already-asserted strap), don't bump.
            self.db.execute_modify(
                """
                UPDATE actuator_channels
                SET last_state = ?,
                    last_changed_at = CURRENT_TIMESTAMP,
                    cycle_count = cycle_count + CASE
                        WHEN last_state IS NULL THEN 1
                        WHEN last_state != ? THEN 1
                        ELSE 0
                    END
                WHERE id = ?
                """,
                (last_state.value, last_state.value, channel_id),
            )
        else:
            self.db.execute_modify(
                """
                UPDATE actuator_channels
                SET last_state = ?, last_changed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (last_state.value, channel_id),
            )

    def commit_actuation(
        self,
        channel_id: int,
        last_state: ChannelState,
        binding_id: int,
        desired_state: DesiredState,
    ) -> None:
        """Atomically stamp channel last_state + binding desired_state.

        The two writes that complete a verb (channel state and binding
        intent) must land together: process kill between them would
        leave ``last_state`` updated but ``desired_state`` stale, and
        the daemon-restart safe-drive would then drive the channel
        backwards — exactly the failure mode safe-drive was meant to
        prevent. Held inside one DB transaction so a crash either
        commits both or rolls back both.
        """
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE actuator_channels
                SET last_state = ?,
                    last_changed_at = CURRENT_TIMESTAMP,
                    cycle_count = cycle_count + CASE
                        WHEN last_state IS NULL THEN 1
                        WHEN last_state != ? THEN 1
                        ELSE 0
                    END
                WHERE id = ?
                """,
                (last_state.value, last_state.value, channel_id),
            )
            conn.execute(
                "UPDATE bindings SET desired_state = ? WHERE id = ?",
                (desired_state.value, binding_id),
            )

    # --- Binding Operations ---

    def create_binding(
        self,
        sbc_id: int,
        purpose: str,
        actuator_channel_id: int,
        *,
        shape_mode: ShapeMode,
        shape_active: ChannelState,
        momentary_pulse_ms: Optional[int] = None,
        sample_phase: SamplePhase = SamplePhase.NONE,
        notes: Optional[str] = None,
    ) -> Binding:
        if sample_phase is SamplePhase.POST_POWER:
            raise ValueError(POST_POWER_REFUSED)
        binding_id = self.db.execute_insert(
            """
            INSERT INTO bindings
              (sbc_id, purpose, actuator_channel_id,
               shape_mode, shape_active,
               momentary_pulse_ms, sample_phase, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sbc_id,
                purpose,
                actuator_channel_id,
                shape_mode.value,
                shape_active.value,
                momentary_pulse_ms,
                sample_phase.value,
                notes,
            ),
        )
        sbc = self.get_sbc(sbc_id)
        sbc_name = sbc.name if sbc else None
        self._audit_log(
            "create",
            "binding",
            binding_id,
            f"{sbc_name}:{purpose}" if sbc_name else purpose,
            f"Bound {purpose!r} on {sbc_name!r} → channel id {actuator_channel_id}",
        )
        result = self.get_binding(binding_id)
        if result is None:
            raise RuntimeError(f"Failed to retrieve binding {binding_id}")
        return result

    def get_binding(self, binding_id: int) -> Optional[Binding]:
        row = self.db.execute_one(
            """
            SELECT b.*, s.name AS sbc_name,
                   a.name AS actuator_name, c.channel_index AS channel_index
            FROM bindings b
            LEFT JOIN sbcs s ON s.id = b.sbc_id
            LEFT JOIN actuator_channels c ON c.id = b.actuator_channel_id
            LEFT JOIN actuators a ON a.id = c.actuator_id
            WHERE b.id = ?
            """,
            (binding_id,),
        )
        return Binding.from_row(row) if row else None

    def get_binding_by_target(self, sbc_id: int, purpose: str) -> Optional[Binding]:
        row = self.db.execute_one(
            """
            SELECT b.*, s.name AS sbc_name,
                   a.name AS actuator_name, c.channel_index AS channel_index
            FROM bindings b
            LEFT JOIN sbcs s ON s.id = b.sbc_id
            LEFT JOIN actuator_channels c ON c.id = b.actuator_channel_id
            LEFT JOIN actuators a ON a.id = c.actuator_id
            WHERE b.sbc_id = ? AND b.purpose = ?
            """,
            (sbc_id, purpose),
        )
        return Binding.from_row(row) if row else None

    def list_bindings(
        self,
        sbc_id: Optional[int] = None,
        channel_ids: Optional[Iterable[int]] = None,
    ) -> list[Binding]:
        sql = (
            "SELECT b.*, s.name AS sbc_name,"
            " a.name AS actuator_name, c.channel_index AS channel_index"
            " FROM bindings b"
            " LEFT JOIN sbcs s ON s.id = b.sbc_id"
            " LEFT JOIN actuator_channels c ON c.id = b.actuator_channel_id"
            " LEFT JOIN actuators a ON a.id = c.actuator_id"
        )
        where: list[str] = []
        params: list = []
        if sbc_id is not None:
            where.append("b.sbc_id = ?")
            params.append(sbc_id)
        if channel_ids is not None:
            ids = list(channel_ids)
            if not ids:
                return []
            where.append(f"b.actuator_channel_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.name, b.purpose"
        return [Binding.from_row(r) for r in self.db.execute(sql, tuple(params))]

    def delete_binding(self, binding_id: int) -> bool:
        binding = self.get_binding(binding_id)
        if not binding:
            return False
        count = self.db.execute_modify(
            "DELETE FROM bindings WHERE id = ?", (binding_id,)
        )
        if count > 0:
            label = (
                f"{binding.sbc_name}:{binding.purpose}"
                if binding.sbc_name
                else binding.purpose
            )
            self._audit_log(
                "delete",
                "binding",
                binding_id,
                label,
                f"Removed binding {label}",
            )
            return True
        return False

    def update_binding_desired_state(
        self, binding_id: int, desired: DesiredState
    ) -> None:
        self.db.execute_modify(
            "UPDATE bindings SET desired_state = ? WHERE id = ?",
            (desired.value, binding_id),
        )

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
        the manager. `actor`, `source` and `agent` are pulled from the
        audit contextvars.
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


def get_manager(
    db_path: Path,
    *,
    timeout_seconds: float = DEFAULT_DB_TIMEOUT,
    base_tcp_port: int = 4000,
) -> ResourceManager:
    """
    Get a resource manager instance.

    Args:
        db_path: Path to database file.
        timeout_seconds: SQLite busy-wait passed to the underlying
            ``Database`` (default matches Database default).
        base_tcp_port: Lowest auto-assigned TCP port (``serial.base_tcp_port``).

    Returns:
        Initialized ResourceManager instance.
    """
    db = get_database(db_path, timeout_seconds=timeout_seconds)
    return ResourceManager(db, base_tcp_port=base_tcp_port)
