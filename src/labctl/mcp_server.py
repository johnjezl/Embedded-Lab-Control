"""
MCP (Model Context Protocol) server for lab controller.

Exposes lab resources, power control, health checks, and SBC management
as MCP resources, tools, and prompts for AI assistant integration.

Usage:
    labctl mcp                    # stdio transport (default)
    labctl mcp --http 8080        # streamable HTTP transport

    # Or directly:
    python -m labctl.mcp_server
"""

import json
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

# All logging must go to stderr (stdout is the JSON-RPC channel for stdio transport)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "labctl",
    instructions=(
        "Lab Controller MCP server. Provides access to embedded development "
        "lab resources including SBC management, power control, serial ports, "
        "and health monitoring. Use resources to read state, tools to perform actions."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_manager():
    """Get a ResourceManager instance."""
    from labctl.core.config import load_config
    from labctl.core.manager import get_manager

    config = load_config()
    return get_manager(config.database_path)


def _sbc_to_dict(sbc) -> dict:
    """Convert an SBC model to a JSON-serializable dict."""
    return sbc.to_dict(include_ids=False)


# ---------------------------------------------------------------------------
# Resources (read-only data)
# ---------------------------------------------------------------------------


@mcp.resource("lab://sbcs")
def list_sbcs() -> str:
    """List all SBCs in the lab with their status, project, IP, and power plug info."""
    manager = _get_manager()
    sbcs = manager.list_sbcs()
    return json.dumps([_sbc_to_dict(s) for s in sbcs], indent=2)


@mcp.resource("lab://sbcs/{sbc_name}")
def get_sbc_details(sbc_name: str) -> str:
    """Get full details for a specific SBC including serial ports, network, and power."""
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})
    return json.dumps(_sbc_to_dict(sbc), indent=2)


@mcp.resource("lab://power/{sbc_name}")
def get_power_state(sbc_name: str) -> str:
    """Get current power state for an SBC (on/off/unknown)."""
    from labctl.power import PowerController, PowerState

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})
    if not sbc.power_plug:
        return json.dumps({"error": f"No power plug assigned to '{sbc_name}'"})

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        state = controller.get_state()
        return json.dumps({
            "sbc": sbc_name,
            "state": state.value,
            "plug_type": sbc.power_plug.plug_type.value,
            "address": sbc.power_plug.address,
        })
    except RuntimeError as e:
        return json.dumps({"sbc": sbc_name, "state": "error", "error": str(e)})


@mcp.resource("lab://serial-devices")
def list_serial_devices() -> str:
    """List all registered USB-serial adapters."""
    manager = _get_manager()
    devices = manager.list_serial_devices()
    return json.dumps(
        [
            {
                "name": d.name,
                "usb_path": d.usb_path,
                "vendor": d.vendor,
                "model": d.model,
                "serial_number": d.serial_number,
            }
            for d in devices
        ],
        indent=2,
    )


@mcp.resource("lab://ports")
def list_ports() -> str:
    """List all serial port assignments with aliases and SBC mappings."""
    manager = _get_manager()
    ports = manager.list_serial_ports()
    sbc_names = {s.id: s.name for s in manager.list_sbcs()}
    return json.dumps(
        [
            {
                "sbc": sbc_names.get(p.sbc_id, f"#{p.sbc_id}"),
                "type": p.port_type.value,
                "alias": p.alias,
                "device": p.device_path,
                "tcp_port": p.tcp_port,
                "baud_rate": p.baud_rate,
                "serial_device": p.serial_device.name if p.serial_device else None,
            }
            for p in ports
        ],
        indent=2,
    )


@mcp.resource("lab://health/{sbc_name}")
def get_health(sbc_name: str) -> str:
    """Run a live health check on an SBC and return results."""
    from labctl.core.config import load_config
    from labctl.health.checks import HealthChecker

    manager = _get_manager()
    config = load_config()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})

    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )
    summary = checker.check_sbc(sbc)

    return json.dumps(
        {
            "sbc": sbc_name,
            "ping": {
                "success": summary.ping_result.success,
                "message": summary.ping_result.message,
            }
            if summary.ping_result
            else None,
            "serial": {
                "success": summary.serial_result.success,
                "message": summary.serial_result.message,
            }
            if summary.serial_result
            else None,
            "power": {
                "state": summary.power_state.value if summary.power_state else None,
                "success": summary.power_result.success
                if summary.power_result
                else None,
                "message": summary.power_result.message
                if summary.power_result
                else None,
            },
            "recommended_status": summary.recommended_status.value
            if summary.recommended_status
            else None,
        },
        indent=2,
    )


@mcp.resource("lab://status")
def get_status_overview() -> str:
    """Get a dashboard-style overview of all SBCs with power states."""
    from labctl.power import PowerController, PowerState

    manager = _get_manager()
    sbcs = manager.list_sbcs()
    result = []

    for sbc in sbcs:
        entry = {
            "name": sbc.name,
            "project": sbc.project or "-",
            "status": sbc.status.value,
            "ip": sbc.primary_ip or "-",
            "power": "-",
        }

        if sbc.power_plug:
            try:
                controller = PowerController.from_plug(sbc.power_plug)
                state = controller.get_state()
                entry["power"] = state.value
            except Exception:
                entry["power"] = "error"

        result.append(entry)

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tools (actions)
# ---------------------------------------------------------------------------


@mcp.tool()
def power_on(sbc_name: str) -> str:
    """Turn on power to an SBC.

    Args:
        sbc_name: Name of the SBC to power on
    """
    from labctl.power import PowerController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if controller.power_on():
            return f"Power ON: {sbc_name}"
        return f"Failed to power on {sbc_name}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def power_off(sbc_name: str) -> str:
    """Turn off power to an SBC.

    Args:
        sbc_name: Name of the SBC to power off
    """
    from labctl.power import PowerController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if controller.power_off():
            return f"Power OFF: {sbc_name}"
        return f"Failed to power off {sbc_name}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def power_cycle(sbc_name: str, delay: float = 3.0) -> str:
    """Power cycle an SBC (turn off, wait, turn on).

    Args:
        sbc_name: Name of the SBC to power cycle
        delay: Seconds to wait between off and on (default 3.0)
    """
    if delay < 0:
        return "Error: delay must be non-negative"

    from labctl.power import PowerController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if controller.power_cycle(delay):
            return f"Power cycled: {sbc_name} (delay: {delay}s)"
        return f"Failed to power cycle {sbc_name}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def run_health_check(sbc_name: Optional[str] = None) -> str:
    """Run health checks on one or all SBCs and return results.

    Args:
        sbc_name: Name of a specific SBC to check, or omit for all SBCs
    """
    from labctl.core.config import load_config
    from labctl.health.checks import HealthChecker

    manager = _get_manager()
    config = load_config()

    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )

    if sbc_name:
        sbc = manager.get_sbc_by_name(sbc_name)
        if not sbc:
            return f"Error: SBC '{sbc_name}' not found"
        sbcs = [sbc]
    else:
        sbcs = manager.list_sbcs()

    results = checker.check_all(sbcs)

    output = []
    for name, summary in results.items():
        entry = {"sbc": name}
        if summary.ping_result:
            entry["ping"] = summary.ping_result.success
        if summary.serial_result:
            entry["serial"] = summary.serial_result.success
        if summary.power_state:
            entry["power"] = summary.power_state.value
        if summary.recommended_status:
            entry["status"] = summary.recommended_status.value
        output.append(entry)

    return json.dumps(output, indent=2)


@mcp.tool()
def add_sbc(
    name: str,
    project: Optional[str] = None,
    description: Optional[str] = None,
    ssh_user: str = "root",
) -> str:
    """Create a new SBC record in the lab inventory.

    Args:
        name: Unique name for the SBC (e.g., 'rpi4-01', 'jetson-nano-2')
        project: Project this SBC belongs to
        description: Human-readable description
        ssh_user: SSH username for remote access (default: root)
    """
    manager = _get_manager()

    try:
        sbc = manager.create_sbc(
            name=name,
            project=project,
            description=description,
            ssh_user=ssh_user,
        )
        return json.dumps({"created": _sbc_to_dict(sbc)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def remove_sbc(name: str) -> str:
    """Remove an SBC and all its assignments from the lab inventory.

    Args:
        name: Name of the SBC to remove
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        return f"Error: SBC '{name}' not found"

    if manager.delete_sbc(sbc.id):
        return f"Removed SBC: {name}"
    return f"Failed to remove SBC: {name}"


@mcp.tool()
def update_sbc(
    name: str,
    rename: Optional[str] = None,
    project: Optional[str] = None,
    description: Optional[str] = None,
    ssh_user: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Update an SBC's properties.

    Args:
        name: Current name of the SBC
        rename: New name for the SBC (optional)
        project: New project name (optional)
        description: New description (optional)
        ssh_user: New SSH username (optional)
        status: New status: unknown, online, offline, booting, error (optional)
    """
    from labctl.core.models import Status

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        return f"Error: SBC '{name}' not found"

    status_enum = Status(status) if status else None

    try:
        updated = manager.update_sbc(
            sbc.id,
            name=rename,
            project=project,
            description=description,
            ssh_user=ssh_user,
            status=status_enum,
        )
        return json.dumps({"updated": _sbc_to_dict(updated)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def assign_serial_port(
    sbc_name: str,
    port_type: str,
    device: str,
    alias: Optional[str] = None,
    baud_rate: int = 115200,
) -> str:
    """Assign a serial port to an SBC.

    Args:
        sbc_name: Name of the SBC
        port_type: Type of port: console, jtag, or debug
        device: Device path (e.g., /dev/lab/port-1)
        alias: Human-friendly name for this connection (e.g., jetson-console)
        baud_rate: Baud rate (default: 115200)
    """
    from labctl.core.models import PortType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    try:
        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType(port_type),
            device_path=device,
            baud_rate=baud_rate,
            alias=alias,
        )
        return f"Assigned {port_type} port to {sbc_name}: {device} (tcp:{port.tcp_port})"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool()
def assign_power_plug(
    sbc_name: str,
    plug_type: str,
    address: str,
    index: int = 1,
) -> str:
    """Assign a smart power plug to an SBC for remote power control.

    Args:
        sbc_name: Name of the SBC
        plug_type: Type of plug: tasmota, kasa, or shelly
        address: IP address or hostname of the plug
        index: Outlet index for multi-outlet strips (default: 1)
    """
    from labctl.core.models import PlugType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    try:
        plug = manager.assign_power_plug(
            sbc_id=sbc.id,
            plug_type=PlugType(plug_type),
            address=address,
            plug_index=index,
        )
        idx = f"[{plug.plug_index}]" if plug.plug_index > 1 else ""
        return f"Assigned {plug_type} plug to {sbc_name}: {address}{idx}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool()
def set_network_address(
    sbc_name: str,
    address_type: str,
    ip_address: str,
    mac: Optional[str] = None,
    hostname: Optional[str] = None,
) -> str:
    """Set a network address for an SBC (used for ping health checks).

    Args:
        sbc_name: Name of the SBC
        address_type: Type of connection: ethernet or wifi
        ip_address: IP address (e.g., 192.168.1.100)
        mac: MAC address (optional)
        hostname: Hostname (optional)
    """
    from labctl.core.models import AddressType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    try:
        addr = manager.set_network_address(
            sbc_id=sbc.id,
            address_type=AddressType(address_type),
            ip_address=ip_address,
            mac_address=mac,
            hostname=hostname,
        )
        return f"Set {address_type} address for {sbc_name}: {addr.ip_address}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Remove / Unassign Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def remove_serial_port(sbc_name: str, port_type: str = "console") -> str:
    """Remove a serial port assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
        port_type: Type of port to remove: console, jtag, or debug
    """
    from labctl.core.models import PortType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    try:
        if manager.remove_serial_port(sbc.id, PortType(port_type)):
            return f"Removed {port_type} port from {sbc_name}"
        return f"No {port_type} port assigned to {sbc_name}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool()
def remove_network_address(sbc_name: str, address_type: str = "ethernet") -> str:
    """Remove a network address from an SBC.

    Args:
        sbc_name: Name of the SBC
        address_type: Type of address to remove: ethernet or wifi
    """
    from labctl.core.models import AddressType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    try:
        if manager.remove_network_address(sbc.id, AddressType(address_type)):
            return f"Removed {address_type} address from {sbc_name}"
        return f"No {address_type} address assigned to {sbc_name}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool()
def remove_power_plug(sbc_name: str) -> str:
    """Remove power plug assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    if manager.remove_power_plug(sbc.id):
        return f"Removed power plug from {sbc_name}"
    return f"No power plug assigned to {sbc_name}"


# ---------------------------------------------------------------------------
# Serial Device CRUD Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def add_serial_device(
    name: str,
    usb_path: str,
    vendor: str | None = None,
    model: str | None = None,
    serial_number: str | None = None,
) -> str:
    """Register a USB-serial adapter.

    Args:
        name: Friendly name for the device (e.g., port-1)
        usb_path: USB bus path (e.g., 1-10.1.3)
        vendor: Manufacturer (optional)
        model: Device model (optional)
        serial_number: Device serial number (optional)
    """
    manager = _get_manager()
    try:
        device = manager.create_serial_device(
            name=name,
            usb_path=usb_path,
            vendor=vendor,
            model=model,
            serial_number=serial_number,
        )
        return f"Registered serial device: {device.name} (usb: {device.usb_path})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def remove_serial_device(name: str) -> str:
    """Unregister a USB-serial adapter.

    Args:
        name: Name of the serial device to remove
    """
    manager = _get_manager()
    device = manager.get_serial_device_by_name(name)
    if not device:
        return f"Error: Serial device '{name}' not found"

    try:
        manager.delete_serial_device(device.id)
        return f"Removed serial device: {name}"
    except ValueError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# SDWire Device CRUD Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def sdwire_add(
    name: str,
    serial_number: str,
    device_type: str = "sdwirec",
) -> str:
    """Register an SDWire SD card multiplexer device.

    Args:
        name: Friendly name (e.g., pi5-sdwire)
        serial_number: Device serial number from discovery
        device_type: Type: sdwirec (Realtek) or sdwire (FTDI legacy)
    """
    manager = _get_manager()
    try:
        device = manager.create_sdwire_device(
            name=name,
            serial_number=serial_number,
            device_type=device_type,
        )
        return f"Registered SDWire device: {device.name} ({device.serial_number})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sdwire_remove(name: str) -> str:
    """Unregister an SDWire device.

    Args:
        name: Name of the SDWire device to remove
    """
    manager = _get_manager()
    device = manager.get_sdwire_device_by_name(name)
    if not device:
        return f"Error: SDWire device '{name}' not found"

    try:
        manager.delete_sdwire_device(device.id)
        return f"Removed SDWire device: {name}"
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def sdwire_assign(sbc_name: str, device_name: str) -> str:
    """Assign an SDWire device to an SBC.

    Args:
        sbc_name: Name of the SBC
        device_name: Name of the SDWire device
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    device = manager.get_sdwire_device_by_name(device_name)
    if not device:
        return f"Error: SDWire device '{device_name}' not found"

    try:
        manager.assign_sdwire(sbc.id, device.id)
        return f"Assigned SDWire '{device_name}' to {sbc_name}"
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def sdwire_unassign(sbc_name: str) -> str:
    """Remove SDWire assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"

    if manager.unassign_sdwire(sbc.id):
        return f"Removed SDWire assignment from {sbc_name}"
    return f"No SDWire assigned to {sbc_name}"


@mcp.tool()
def sdwire_discover() -> str:
    """Discover connected SDWire devices.

    Scans for both SDWireC (Realtek) and legacy SDWire (FTDI) devices.
    Returns device serial numbers and types for registration.
    """
    try:
        from labctl.sdwire.controller import discover_sdwire_devices

        devices = discover_sdwire_devices()
        if not devices:
            return "No SDWire devices found"
        return json.dumps(devices, indent=2)
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def serial_discover() -> str:
    """Discover connected USB-serial adapters.

    Scans for ttyUSB and ttyACM devices and returns their USB paths,
    vendor, model, and serial number for registration.
    """
    try:
        from labctl.serial.udev import discover_usb_serial

        devices = discover_usb_serial()
        if not devices:
            return "No USB-serial devices found"
        return json.dumps(devices, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("lab://sdwire-devices")
def list_sdwire_devices() -> str:
    """List all registered SDWire SD card multiplexer devices."""
    manager = _get_manager()
    devices = manager.list_sdwire_devices()
    sbcs = manager.list_sbcs()
    assigned = {}
    for sbc in sbcs:
        if sbc.sdwire:
            assigned[sbc.sdwire.id] = sbc.name
    return json.dumps(
        [
            {
                "name": d.name,
                "serial_number": d.serial_number,
                "assigned_to": assigned.get(d.id),
            }
            for d in devices
        ],
        indent=2,
    )


@mcp.tool()
def sdwire_to_dut(sbc_name: str) -> str:
    """Switch an SBC's SD card to DUT mode (SBC boots from the SD card).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_dut()
        return f"SD card switched to DUT: {sbc_name}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def sdwire_to_host(sbc_name: str, force: bool = False) -> str:
    """Switch an SBC's SD card to host mode (dev machine can read/write the SD card).

    Refuses if the SBC is powered on to prevent SD card bus contention.
    Use force=True to override (e.g., if SBC is halted but power relay is on).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        force: Override power-on safety check
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    # Power safety check
    if not force and sbc.power_plug:
        try:
            from labctl.power import PowerController
            power_ctrl = PowerController.from_plug(sbc.power_plug)
            state = power_ctrl.get_state()
            from labctl.power.base import PowerState
            if state == PowerState.ON:
                return (
                    f"Error: {sbc_name} is powered on. Power off before "
                    f"switching SD to host mode. Use force=true to override "
                    f"(risks SD card corruption)."
                )
        except Exception:
            pass  # Power state unknown — allow operation

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_host()
        block_dev = ctrl.get_block_device()
        msg = f"SD card switched to host: {sbc_name}"
        if block_dev:
            msg += f" (block device: {block_dev})"
        return msg
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def sdwire_update(
    sbc_name: str,
    partition: int,
    copies: list[str] = [],
    renames: list[str] = [],
    deletes: list[str] = [],
    reboot: bool = False,
) -> str:
    """Copy, rename, and/or delete files on a partition on an SBC's SD card.

    Atomic operation: switches SD to host, mounts partition, performs operations
    (copies first, then renames, then deletes), unmounts, switches back to DUT,
    optionally power cycles.

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        partition: Partition number (e.g., 1 for the first partition)
        copies: List of "source:dest" pairs (dest is relative to partition root)
        renames: List of "oldname:newname" pairs (both relative to partition root)
        deletes: List of filenames to delete (relative to partition root)
        reboot: Whether to power cycle the SBC after updating
    """
    import time

    from labctl.sdwire import SDWireController

    if not copies and not renames and not deletes:
        return "Error: At least one of copies, renames, or deletes is required"

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    # Parse copy pairs
    file_pairs = []
    for spec in copies:
        if ":" not in spec:
            return f"Error: Invalid copy format '{spec}'. Use source:dest"
        src, dest = spec.split(":", 1)
        file_pairs.append((src, dest))

    # Parse rename pairs
    rename_pairs = []
    for spec in renames:
        if ":" not in spec:
            return f"Error: Invalid rename format '{spec}'. Use oldname:newname"
        old_name, new_name = spec.split(":", 1)
        rename_pairs.append((old_name, new_name))

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

    try:
        # Auto power-off to prevent SD card bus contention
        if sbc.power_plug:
            try:
                from labctl.power import PowerController
                power_ctrl = PowerController.from_plug(sbc.power_plug)
                power_ctrl.power_off()
                time.sleep(1)
            except Exception:
                pass  # Best effort — continue even if power off fails

        ctrl.switch_to_host()
        time.sleep(2)

        result = ctrl.update_files(
            partition, file_pairs,
            renames=rename_pairs or None,
            deletes=list(deletes) or None,
        )

        ctrl.switch_to_dut()

        parts = []
        if result["copied"]:
            parts.append(f"Copied: {', '.join(result['copied'])}")
        if result["renamed"]:
            parts.append(f"Renamed: {', '.join(result['renamed'])}")
        if result["deleted"]:
            parts.append(f"Deleted: {', '.join(result['deleted'])}")
        summary = f"Partition {partition}: {'; '.join(parts)}"

        if reboot and sbc.power_plug:
            from labctl.power import PowerController

            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_cycle()
            summary += f". Power cycled {sbc_name}."

        return summary
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def flash_image(
    sbc_name: str,
    image_path: str,
    reboot: bool = False,
    post_flash_copies: list[str] = [],
) -> str:
    """Flash a raw disk image to an SBC's SD card via SDWire.

    Supports .img, .img.xz, and .img.gz formats. Includes safety checks:
    verifies block device size, ensures no partitions are mounted, and
    resolves the device from SDWire config (never accepts raw /dev paths).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        image_path: Absolute path to image file (.img, .img.xz, .img.gz)
        reboot: Power on the SBC after flashing
        post_flash_copies: Optional "source:dest" pairs to copy to boot partition after flash
    """
    import time as time_mod

    from labctl.sdwire import SDWireController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    flash_ok = False

    try:
        # Power off (best effort)
        if sbc.power_plug:
            try:
                from labctl.power import PowerController
                power_ctrl = PowerController.from_plug(sbc.power_plug)
                power_ctrl.power_off()
                time_mod.sleep(1)
            except Exception:
                pass  # Continue even if power off fails

        # Switch SD to host and wait for device
        ctrl.switch_to_host()
        time_mod.sleep(2)

        block_dev = ctrl.get_block_device(settle_time=2)
        if not block_dev:
            block_dev = ctrl.get_block_device(settle_time=5)
        if not block_dev:
            return "Error: Block device not found after switching to host (waited 10s)"

        # Flash image (includes safety validation)
        result = ctrl.flash_image(image_path)
        flash_ok = True

        parts = [
            f"Flashed {image_path} to {result['block_device']}",
            f"{result['bytes_written']} bytes in {result['elapsed_seconds']}s",
        ]

        # Post-flash partition copies
        if post_flash_copies:
            try:
                import subprocess
                subprocess.run(
                    ["sudo", "partprobe", block_dev],
                    capture_output=True, timeout=10,
                )
                time_mod.sleep(2)

                file_pairs = []
                for spec in post_flash_copies:
                    if ":" not in spec:
                        parts.append(f"Skipped invalid copy: {spec}")
                        continue
                    src, dest = spec.split(":", 1)
                    file_pairs.append((src, dest))

                if file_pairs:
                    copied = ctrl.update_files(1, file_pairs)
                    for f in copied["copied"]:
                        parts.append(f"Post-flash copied: {f}")
            except RuntimeError as e:
                parts.append(f"Post-flash copy error: {e}")

        # Switch back to DUT
        ctrl.switch_to_dut()

        # Reboot
        if reboot and sbc.power_plug:
            from labctl.power import PowerController
            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_on()
            parts.append(f"Powered on {sbc_name}")

        return ". ".join(parts)

    except RuntimeError as e:
        if not flash_ok:
            return f"Error: {e}. SD card left on host for inspection."
        try:
            ctrl.switch_to_dut()
        except Exception:
            pass
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Serial I/O Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def serial_capture(
    port_name: str,
    timeout: float = 15.0,
    until_pattern: str | None = None,
    tail: int | None = None,
) -> str:
    """Capture serial output from a port.

    Connects to the serial port via ser2net, captures output until timeout
    or pattern match, then disconnects.

    Args:
        port_name: Port alias or SBC name (SBC defaults to console port)
        timeout: Max seconds to capture (default: 15)
        until_pattern: Regex to stop on when matched (per line)
        tail: Return only last N lines (None = all)
    """
    from labctl.serial.capture import (
        capture_serial_output,
        resolve_port,
    )

    manager = _get_manager()

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        return f"Error: {e}"

    if not port.tcp_port:
        return f"Error: Port '{port_name}' has no TCP port configured"

    try:
        result = capture_serial_output(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            timeout=timeout,
            until_pattern=until_pattern,
            tail=tail,
        )
        return result.to_mcp_string(pattern=until_pattern)
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def serial_send(
    port_name: str,
    data: str,
    newline: bool = True,
    capture_timeout: float | None = None,
    capture_until: str | None = None,
) -> str:
    """Send data to a serial port, optionally capturing the response.

    Connects to the serial port via ser2net, sends data, optionally
    captures the response, then disconnects.

    Args:
        port_name: Port alias or SBC name (SBC defaults to console port)
        data: String data to send
        newline: Append \\r\\n after data (default: True)
        capture_timeout: If set, capture response for this many seconds
        capture_until: If set, capture until this regex matches a line
    """
    from labctl.serial.capture import (
        resolve_port,
        send_serial_data,
    )

    manager = _get_manager()

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        return f"Error: {e}"

    if not port.tcp_port:
        return f"Error: Port '{port_name}' has no TCP port configured"

    try:
        result = send_serial_data(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            data=data,
            newline=newline,
            capture_timeout=capture_timeout,
            capture_until=capture_until,
        )
        return result.to_mcp_string()
    except RuntimeError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Boot Test
# ---------------------------------------------------------------------------


@mcp.tool()
def boot_test(
    sbc_name: str,
    expect_pattern: str,
    runs: int = 10,
    timeout: float = 30.0,
    image: str | None = None,
    dest: str | None = None,
    partition: int = 1,
    output_dir: str | None = None,
) -> str:
    """Automated boot reliability testing.

    Optionally deploys an image, then reboots the SBC multiple times,
    capturing serial output each time. Reports how many boots
    successfully reached the expected pattern.

    Args:
        sbc_name: Name of the SBC to test
        expect_pattern: Regex that indicates successful boot
        runs: Number of boot cycles (default: 10)
        timeout: Seconds to wait per boot (default: 30)
        image: Image file to deploy (None = skip deploy)
        dest: Destination filename on SD card (required with image)
        partition: Partition number for deploy (default: 1)
        output_dir: Save per-run output to files here
    """
    if runs < 1:
        return "Error: runs must be at least 1"
    if timeout <= 0:
        return "Error: timeout must be positive"

    import time as time_mod

    from labctl.serial.boot_test import run_boot_test

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.console_port or not sbc.console_port.tcp_port:
        return f"Error: SBC '{sbc_name}' has no console port with TCP configured"
    if not sbc.power_plug:
        return f"Error: SBC '{sbc_name}' has no power plug assigned"

    if image and not dest:
        return "Error: dest is required when image is specified"

    # Build deploy function
    deploy_fn = None
    if image and dest:
        def deploy_fn():
            from labctl.sdwire import SDWireController

            if not sbc.sdwire:
                raise RuntimeError(f"No SDWire assigned to '{sbc_name}'")
            ctrl = SDWireController(
                sbc.sdwire.serial_number, sbc.sdwire.device_type
            )
            ctrl.switch_to_host()
            time_mod.sleep(2)
            ctrl.update_files(partition, [(image, dest)])
            ctrl.switch_to_dut()

    # Build power cycle function
    def power_cycle_fn():
        from labctl.power import PowerController
        power_ctrl = PowerController.from_plug(sbc.power_plug)
        power_ctrl.power_cycle(delay=3.0)

    try:
        result = run_boot_test(
            sbc_name=sbc_name,
            expect_pattern=expect_pattern,
            tcp_host="localhost",
            tcp_port=sbc.console_port.tcp_port,
            power_cycle_fn=power_cycle_fn,
            runs=runs,
            timeout=timeout,
            deploy_fn=deploy_fn,
            image=image,
            dest=dest,
            partition=partition,
            output_dir=output_dir,
        )
        return result.format_summary()
    except RuntimeError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Prompts (reusable instruction templates)
# ---------------------------------------------------------------------------


@mcp.prompt()
def debug_sbc(sbc_name: str) -> str:
    """Guide through debugging an unresponsive SBC.

    Args:
        sbc_name: Name of the SBC to debug
    """
    return f"""Debug SBC '{sbc_name}' by following these steps:

1. Check the SBC's current state by reading the lab://sbcs/{sbc_name} resource.
2. Check power state by reading the lab://power/{sbc_name} resource.
3. Run a health check using the run_health_check tool with sbc_name="{sbc_name}".

Based on findings:
- If power is OFF: use the power_on tool to turn it on, wait 30 seconds, then re-check health.
- If power is ON but ping fails: the SBC may be hung. Try power_cycle to reboot it.
- If ping succeeds but serial fails: check physical serial cable connections.
- If everything passes but status is wrong: use update_sbc to correct the status.

Report your findings and any actions taken."""


@mcp.prompt()
def lab_report() -> str:
    """Generate a comprehensive lab status report."""
    return """Generate a lab status report by:

1. Read the lab://status resource for an overview of all SBCs.
2. Read the lab://serial-devices resource for registered USB adapters.
3. Read the lab://ports resource for serial port assignments.

Compile a report with:
- Summary: total SBCs, how many online/offline/unknown
- Per-SBC details: name, project, status, IP, power state
- Any issues: SBCs that are offline or in error state
- Serial port utilization: assigned vs unassigned devices
- Recommendations for any problems found"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server(transport: str = "stdio", http_port: int = 8080):
    """Start the MCP server."""
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        mcp.run(transport="streamable-http")
    else:
        raise ValueError(f"Unknown transport: {transport}")


if __name__ == "__main__":
    run_server()
