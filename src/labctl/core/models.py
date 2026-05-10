"""
Data models for lab controller.

Defines dataclasses for SBCs, serial ports, network addresses, and power plugs.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class Status(Enum):
    """SBC status values."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    BOOTING = "booting"
    ERROR = "error"


class PortType(Enum):
    """Serial port type values."""

    CONSOLE = "console"
    JTAG = "jtag"
    DEBUG = "debug"


class AddressType(Enum):
    """Network address type values."""

    ETHERNET = "ethernet"
    WIFI = "wifi"


class PlugType(Enum):
    """Power plug type values."""

    TASMOTA = "tasmota"
    KASA = "kasa"
    SHELLY = "shelly"


class ReleaseReason(str, Enum):
    """Reasons a claim transitioned to released state."""

    RELEASED = "released"
    EXPIRED = "expired"
    FORCE_RELEASED = "force-released"
    SESSION_LOST = "session-lost"


class ClaimError(Exception):
    """Base class for claim-related errors."""


class UnknownSBCError(ClaimError):
    """Referenced SBC does not exist."""


class ClaimConflict(ClaimError):
    """Another session holds an active claim on the target SBC."""

    def __init__(self, claim: "Claim"):
        self.claim = claim
        holder = claim.agent_name or "unknown agent"
        super().__init__(
            f"SBC is claimed by '{holder}' until "
            f"{claim.expires_at.isoformat() if claim.expires_at else 'unknown'}"
        )


class ClaimNotFoundError(ClaimError):
    """No active claim exists on the target SBC."""


class NotClaimantError(ClaimError):
    """The calling session does not hold the active claim."""


class SessionKind(str, Enum):
    """Transport/origin of a claim session."""

    MCP_STDIO = "mcp-stdio"
    MCP_HTTP = "mcp-http"
    CLI = "cli"
    WEB = "web"


@dataclass
class SerialDevice:
    """Physical USB-serial adapter registered in the system."""

    id: Optional[int] = None
    name: str = ""
    usb_path: str = ""
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SerialDevice":
        """Create SerialDevice from database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            usb_path=row["usb_path"],
            vendor=row["vendor"],
            model=row["model"],
            serial_number=row["serial_number"],
            created_at=row["created_at"],
        )


@dataclass
class SerialPort:
    """Serial port assignment linking a serial device to an SBC."""

    id: Optional[int] = None
    sbc_id: int = 0
    port_type: PortType = PortType.CONSOLE
    device_path: str = ""
    tcp_port: Optional[int] = None
    baud_rate: int = 115200
    alias: Optional[str] = None
    serial_device_id: Optional[int] = None
    created_at: Optional[datetime] = None

    # Populated by manager when loading relations
    serial_device: Optional[SerialDevice] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SerialPort":
        """Create SerialPort from database row."""
        keys = row.keys()
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            port_type=PortType(row["port_type"]),
            device_path=row["device_path"],
            tcp_port=row["tcp_port"],
            baud_rate=row["baud_rate"],
            alias=row["alias"] if "alias" in keys else None,
            serial_device_id=(
                row["serial_device_id"] if "serial_device_id" in keys else None
            ),
            created_at=row["created_at"],
        )


@dataclass
class NetworkAddress:
    """Network address for an SBC."""

    id: Optional[int] = None
    sbc_id: int = 0
    address_type: AddressType = AddressType.ETHERNET
    ip_address: str = ""
    mac_address: Optional[str] = None
    hostname: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NetworkAddress":
        """Create NetworkAddress from database row."""
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            address_type=AddressType(row["address_type"]),
            ip_address=row["ip_address"],
            mac_address=row["mac_address"],
            hostname=row["hostname"],
            created_at=row["created_at"],
        )


@dataclass
class PowerPlug:
    """Power plug assignment for an SBC."""

    id: Optional[int] = None
    sbc_id: int = 0
    plug_type: PlugType = PlugType.TASMOTA
    address: str = ""
    plug_index: int = 1
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PowerPlug":
        """Create PowerPlug from database row."""
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            plug_type=PlugType(row["plug_type"]),
            address=row["address"],
            plug_index=row["plug_index"],
            created_at=row["created_at"],
        )


@dataclass
class SDWireDevice:
    """SD card multiplexer (SDWire/SDWireC/SDWire3) device."""

    id: Optional[int] = None
    name: str = ""
    serial_number: str = ""
    device_type: str = "sdwirec"  # sdwire, sdwirec, sdwire3
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SDWireDevice":
        """Create SDWireDevice from database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            serial_number=row["serial_number"],
            device_type=row["device_type"],
            created_at=row["created_at"],
        )


@dataclass
class SBC:
    """Single Board Computer record."""

    id: Optional[int] = None
    name: str = ""
    project: Optional[str] = None
    description: Optional[str] = None
    ssh_user: str = "root"
    status: Status = Status.UNKNOWN
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Cached power observation written by the monitor daemon every cycle.
    # Read by `labctl status --fast` to avoid live network probes.
    last_power_state: Optional[str] = None  # "on" | "off" | "unknown"
    last_power_at: Optional[str] = None

    # Per-SBC power-cycle off→on delay. None means "use the global default".
    power_cycle_delay_seconds: Optional[float] = None

    # Related objects (populated by manager)
    serial_ports: list[SerialPort] = field(default_factory=list)
    network_addresses: list[NetworkAddress] = field(default_factory=list)
    power_plug: Optional[PowerPlug] = None
    sdwire: Optional[SDWireDevice] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SBC":
        """Create SBC from database row."""
        # Pre-v6 rows may not have the cached-power columns; tolerate that.
        try:
            last_power_state = row["last_power_state"]
        except (IndexError, KeyError):
            last_power_state = None
        try:
            last_power_at = row["last_power_at"]
        except (IndexError, KeyError):
            last_power_at = None
        # Pre-v7 rows lack the cycle-delay column.
        try:
            power_cycle_delay_seconds = row["power_cycle_delay_seconds"]
        except (IndexError, KeyError):
            power_cycle_delay_seconds = None
        return cls(
            id=row["id"],
            name=row["name"],
            project=row["project"],
            description=row["description"],
            ssh_user=row["ssh_user"],
            status=Status(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_power_state=last_power_state,
            last_power_at=last_power_at,
            power_cycle_delay_seconds=power_cycle_delay_seconds,
        )

    @property
    def console_port(self) -> Optional[SerialPort]:
        """Get console serial port if assigned."""
        for port in self.serial_ports:
            if port.port_type == PortType.CONSOLE:
                return port
        return None

    @property
    def primary_ip(self) -> Optional[str]:
        """Get primary IP address (ethernet preferred)."""
        for addr in self.network_addresses:
            if addr.address_type == AddressType.ETHERNET:
                return addr.ip_address
        if self.network_addresses:
            return self.network_addresses[0].ip_address
        return None

    def to_dict(self, include_ids: bool = False) -> dict:
        """Convert SBC to JSON-serializable dict.

        Args:
            include_ids: Include database IDs in output (for REST API).
        """
        data = {
            "name": self.name,
            "project": self.project,
            "description": self.description,
            "ssh_user": self.ssh_user,
            "status": self.status.value,
            "primary_ip": self.primary_ip,
        }
        if include_ids:
            data["id"] = self.id

        if self.serial_ports:
            data["serial_ports"] = [
                {
                    **({"id": p.id} if include_ids else {}),
                    "type": p.port_type.value,
                    "device": p.device_path,
                    "alias": p.alias,
                    "tcp_port": p.tcp_port,
                    "baud_rate": p.baud_rate,
                    "serial_device": p.serial_device.name if p.serial_device else None,
                }
                for p in self.serial_ports
            ]

        if self.network_addresses:
            data["network_addresses"] = [
                {
                    **({"id": a.id} if include_ids else {}),
                    "type": a.address_type.value,
                    "ip": a.ip_address,
                    "mac": a.mac_address,
                    "hostname": a.hostname,
                }
                for a in self.network_addresses
            ]

        if self.power_plug:
            data["power_plug"] = {
                **({"id": self.power_plug.id} if include_ids else {}),
                "type": self.power_plug.plug_type.value,
                "address": self.power_plug.address,
                "index": self.power_plug.plug_index,
            }

        if self.sdwire:
            data["sdwire"] = {
                "name": self.sdwire.name,
                "serial_number": self.sdwire.serial_number,
                "device_type": self.sdwire.device_type,
            }

        return data


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse a SQLite TIMESTAMP value into a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # SQLite returns timestamps as strings in ISO-like format
    # ("YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS[.ffffff]").
    s = str(value).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class ClaimRequest:
    """A polite release request recorded against an active claim."""

    id: Optional[int] = None
    claim_id: int = 0
    requested_by: str = ""
    reason: str = ""
    requested_at: Optional[datetime] = None
    acknowledged: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ClaimRequest":
        return cls(
            id=row["id"],
            claim_id=row["claim_id"],
            requested_by=row["requested_by"],
            reason=row["reason"],
            requested_at=_parse_timestamp(row["requested_at"]),
            acknowledged=bool(row["acknowledged"]),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "requested_at": (
                self.requested_at.isoformat() if self.requested_at else None
            ),
            "acknowledged": self.acknowledged,
        }


@dataclass
class Claim:
    """Exclusive-access claim on an SBC.

    Expiry is computed as ``last_activity + duration_seconds``; the
    ``expires_at`` column is a materialized view of that rule, rewritten
    alongside ``last_activity`` on every heartbeat.
    """

    id: Optional[int] = None
    sbc_id: int = 0
    agent_name: str = ""
    session_id: str = ""
    session_kind: str = ""
    reason: str = ""
    context: Optional[dict] = None
    acquired_at: Optional[datetime] = None
    duration_seconds: int = 0
    last_activity: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    renewal_count: int = 0
    released_at: Optional[datetime] = None
    release_reason: Optional[ReleaseReason] = None
    released_by: Optional[str] = None
    pending_requests: list[ClaimRequest] = field(default_factory=list)

    # Populated by manager when loading relations
    sbc_name: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Claim":
        keys = row.keys()
        context = None
        if row["context_json"]:
            try:
                context = json.loads(row["context_json"])
            except (json.JSONDecodeError, TypeError):
                context = None
        release_reason = None
        if row["release_reason"]:
            try:
                release_reason = ReleaseReason(row["release_reason"])
            except ValueError:
                release_reason = None
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            agent_name=row["agent_name"],
            session_id=row["session_id"],
            session_kind=row["session_kind"],
            reason=row["reason"],
            context=context,
            acquired_at=_parse_timestamp(row["acquired_at"]),
            duration_seconds=row["duration_seconds"],
            last_activity=_parse_timestamp(row["last_activity"]),
            expires_at=_parse_timestamp(row["expires_at"]),
            renewal_count=row["renewal_count"],
            released_at=_parse_timestamp(row["released_at"]),
            release_reason=release_reason,
            released_by=row["released_by"],
            sbc_name=row["sbc_name"] if "sbc_name" in keys else None,
        )

    @property
    def is_active(self) -> bool:
        """True if the claim is unreleased and within its deadline."""
        if self.released_at is not None:
            return False
        if self.expires_at is not None and datetime.now() > self.expires_at:
            return False
        return True

    @property
    def time_remaining(self) -> Optional[timedelta]:
        if not self.is_active or self.expires_at is None:
            return None
        return self.expires_at - datetime.now()

    def to_dict(self, include_ids: bool = False) -> dict:
        data = {
            "agent_name": self.agent_name,
            "session_kind": self.session_kind,
            "reason": self.reason,
            "acquired_at": (self.acquired_at.isoformat() if self.acquired_at else None),
            "expires_at": (self.expires_at.isoformat() if self.expires_at else None),
            "last_activity": (
                self.last_activity.isoformat() if self.last_activity else None
            ),
            "duration_seconds": self.duration_seconds,
            "renewal_count": self.renewal_count,
            "is_active": self.is_active,
        }
        remaining = self.time_remaining
        if remaining is not None:
            data["time_remaining_seconds"] = max(0, int(remaining.total_seconds()))
        if self.context:
            data["context"] = self.context
        if self.sbc_name:
            data["sbc_name"] = self.sbc_name
        if self.released_at is not None:
            data["released_at"] = self.released_at.isoformat()
            data["release_reason"] = (
                self.release_reason.value if self.release_reason else None
            )
            data["released_by"] = self.released_by
        if self.pending_requests:
            data["pending_requests"] = [r.to_dict() for r in self.pending_requests]
        if include_ids:
            data["id"] = self.id
            data["sbc_id"] = self.sbc_id
        return data


# ---------------------------------------------------------------------------
# Actuators / bindings (schema v8)
# ---------------------------------------------------------------------------


class ActuatorKind(str, Enum):
    """Top-level category of an actuator."""

    RELAY = "relay"
    GPIO = "gpio"


class DriverName(str, Enum):
    """Concrete driver implementations.

    v1 ships ``LCUS1_SERIAL``; the others are placeholders for future
    driver work referenced by the spec but not yet implemented.
    """

    LCUS1_SERIAL = "lcus1_serial"
    NUMATO_ACM = "numato_acm"
    HID_GENERIC = "hid_generic"
    SYSFS_GPIO = "sysfs_gpio"


class ChannelState(str, Enum):
    """Physical state of a relay contact."""

    OPEN = "open"
    CLOSED = "closed"


class ShapeMode(str, Enum):
    """Whether a binding represents sustained or pulsed actuation."""

    LATCH = "latch"
    MOMENTARY = "momentary"


class SamplePhase(str, Enum):
    """When the binding's value is consequential relative to power."""

    PRE_POWER = "pre_power"
    POST_POWER = "post_power"
    NONE = "none"


class DesiredState(str, Enum):
    """Operator intent for a binding (persistent across daemon restarts)."""

    ASSERTED = "asserted"
    RELEASED = "released"
    FOLLOWING_POWER = "following_power"


# Recommended purposes — the binding `purpose` column stores any string,
# but provisioning UIs should default to / suggest these.
KNOWN_PURPOSES = (
    "recovery_mode",
    "boot_select",
    "power_button",
    "reset_button",
    "usb_data_break",
    "usb_power_break",
    "dut_input",
)


@dataclass
class Actuator:
    """A USB-controlled relay (or similar single-bit actuator)."""

    id: Optional[int] = None
    name: str = ""
    kind: ActuatorKind = ActuatorKind.RELAY
    driver: DriverName = DriverName.LCUS1_SERIAL
    device_path: Optional[str] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    serial_no: Optional[str] = None
    last_probe_at: Optional[datetime] = None
    last_probe_result: Optional[str] = None
    created_at: Optional[datetime] = None

    # Populated by the manager when relations are loaded.
    channels: list["ActuatorChannel"] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Actuator":
        return cls(
            id=row["id"],
            name=row["name"],
            kind=ActuatorKind(row["kind"]),
            driver=DriverName(row["driver"]),
            device_path=row["device_path"],
            vid=row["vid"],
            pid=row["pid"],
            serial_no=row["serial_no"],
            last_probe_at=_parse_timestamp(row["last_probe_at"]),
            last_probe_result=row["last_probe_result"],
            created_at=row["created_at"],
        )

    def to_dict(self, include_ids: bool = False) -> dict:
        data: dict = {
            "name": self.name,
            "kind": self.kind.value,
            "driver": self.driver.value,
            "device_path": self.device_path,
            "vid": self.vid,
            "pid": self.pid,
            "serial_no": self.serial_no,
            "last_probe_at": (
                self.last_probe_at.isoformat() if self.last_probe_at else None
            ),
            "last_probe_result": self.last_probe_result,
        }
        if include_ids:
            data["id"] = self.id
        if self.channels:
            data["channels"] = [c.to_dict(include_ids=include_ids) for c in self.channels]
        return data


@dataclass
class ActuatorChannel:
    """One outlet / channel on an actuator."""

    id: Optional[int] = None
    actuator_id: int = 0
    channel_index: int = 1  # 1-based
    label: Optional[str] = None
    default_state: ChannelState = ChannelState.OPEN
    last_state: Optional[ChannelState] = None
    last_changed_at: Optional[datetime] = None
    cycle_count: int = 0
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ActuatorChannel":
        last_raw = row["last_state"]
        return cls(
            id=row["id"],
            actuator_id=row["actuator_id"],
            channel_index=row["channel_index"],
            label=row["label"],
            default_state=ChannelState(row["default_state"]),
            last_state=ChannelState(last_raw) if last_raw else None,
            last_changed_at=_parse_timestamp(row["last_changed_at"]),
            cycle_count=row["cycle_count"],
            created_at=row["created_at"],
        )

    def to_dict(self, include_ids: bool = False) -> dict:
        data: dict = {
            "channel_index": self.channel_index,
            "label": self.label,
            "default_state": self.default_state.value,
            "last_state": self.last_state.value if self.last_state else None,
            "last_changed_at": (
                self.last_changed_at.isoformat() if self.last_changed_at else None
            ),
            "cycle_count": self.cycle_count,
        }
        if include_ids:
            data["id"] = self.id
            data["actuator_id"] = self.actuator_id
        return data


@dataclass
class Binding:
    """A (target SBC, purpose) → actuator-channel association."""

    id: Optional[int] = None
    sbc_id: int = 0
    purpose: str = ""
    actuator_channel_id: int = 0
    shape_mode: ShapeMode = ShapeMode.LATCH
    shape_active: ChannelState = ChannelState.CLOSED
    momentary_pulse_ms: Optional[int] = None
    sample_phase: SamplePhase = SamplePhase.NONE
    desired_state: DesiredState = DesiredState.RELEASED
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    # Populated by manager when joining for display.
    sbc_name: Optional[str] = None
    actuator_name: Optional[str] = None
    channel_index: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Binding":
        keys = row.keys()
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            purpose=row["purpose"],
            actuator_channel_id=row["actuator_channel_id"],
            shape_mode=ShapeMode(row["shape_mode"]),
            shape_active=ChannelState(row["shape_active"]),
            momentary_pulse_ms=row["momentary_pulse_ms"],
            sample_phase=SamplePhase(row["sample_phase"]),
            desired_state=DesiredState(row["desired_state"]),
            notes=row["notes"],
            created_at=row["created_at"],
            sbc_name=row["sbc_name"] if "sbc_name" in keys else None,
            actuator_name=(
                row["actuator_name"] if "actuator_name" in keys else None
            ),
            channel_index=(
                row["channel_index"] if "channel_index" in keys else None
            ),
        )

    def to_dict(self, include_ids: bool = False) -> dict:
        data: dict = {
            "purpose": self.purpose,
            "shape": {
                "mode": self.shape_mode.value,
                "active": self.shape_active.value,
                "momentary_pulse_ms": self.momentary_pulse_ms,
                "sample_phase": self.sample_phase.value,
            },
            "desired_state": self.desired_state.value,
            "notes": self.notes,
        }
        if self.sbc_name:
            data["sbc_name"] = self.sbc_name
        if self.actuator_name:
            data["actuator_name"] = self.actuator_name
        if self.channel_index is not None:
            data["channel_index"] = self.channel_index
        if include_ids:
            data["id"] = self.id
            data["sbc_id"] = self.sbc_id
            data["actuator_channel_id"] = self.actuator_channel_id
        return data
