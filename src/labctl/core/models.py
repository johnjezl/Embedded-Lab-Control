"""
Data models for lab controller.

Defines dataclasses for SBCs, serial ports, network addresses, and power plugs.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
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


@dataclass
class SerialPort:
    """Serial port assignment."""

    id: Optional[int] = None
    sbc_id: int = 0
    port_type: PortType = PortType.CONSOLE
    device_path: str = ""
    tcp_port: Optional[int] = None
    baud_rate: int = 115200
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SerialPort":
        """Create SerialPort from database row."""
        return cls(
            id=row["id"],
            sbc_id=row["sbc_id"],
            port_type=PortType(row["port_type"]),
            device_path=row["device_path"],
            tcp_port=row["tcp_port"],
            baud_rate=row["baud_rate"],
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

    # Related objects (populated by manager)
    serial_ports: list[SerialPort] = field(default_factory=list)
    network_addresses: list[NetworkAddress] = field(default_factory=list)
    power_plug: Optional[PowerPlug] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SBC":
        """Create SBC from database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            project=row["project"],
            description=row["description"],
            ssh_user=row["ssh_user"],
            status=Status(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
