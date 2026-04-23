"""
Command-line interface for lab controller.

Provides commands for managing serial ports, connections, and lab resources.
"""

import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import click

from labctl import __version__
from labctl.core import audit
from labctl.core.config import Config, load_config
from labctl.core.manager import ResourceManager, get_manager
from labctl.core.models import AddressType, PlugType, PortType, Status
from labctl.power import PowerController, PowerState
from labctl.serial.ser2net import Ser2NetPort, generate_ser2net_config

# Command aliases mapping
ALIASES = {
    "ls": "list",
    "rm": "remove",
    "delete": "remove",
    "show": "info",
    "on": "power on",
    "off": "power off",
}

STATUS_POWER_CACHE_TTL = 2.0
_status_power_cache: dict[tuple[str, str, int], tuple[float, str]] = {}
_status_power_cache_lock = threading.Lock()


class AliasedGroup(click.Group):
    """Custom Click group that supports command aliases."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # First try the actual command
        cmd = click.Group.get_command(self, ctx, cmd_name)
        if cmd is not None:
            return cmd

        # Check for alias
        if cmd_name in ALIASES:
            actual_cmd = ALIASES[cmd_name]
            # Handle multi-word aliases (like "power on")
            if " " in actual_cmd:
                # For multi-word, we need to get the subcommand
                parts = actual_cmd.split()
                cmd = click.Group.get_command(self, ctx, parts[0])
                if cmd and isinstance(cmd, click.Group):
                    return cmd.get_command(ctx, parts[1])
            else:
                return click.Group.get_command(self, ctx, actual_cmd)

        return None

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Handle multi-word aliases before resolution
        if args and args[0] in ALIASES:
            alias = args[0]
            actual_cmd = ALIASES[alias]
            if " " in actual_cmd:
                # Replace alias with the actual command words
                parts = actual_cmd.split()
                args = parts + args[1:]

        return super().resolve_command(ctx, args)


def _get_manager(ctx: click.Context) -> ResourceManager:
    """Get or create resource manager from context."""
    if "manager" not in ctx.obj:
        config: Config = ctx.obj["config"]
        ctx.obj["manager"] = get_manager(config.database_path)
    return ctx.obj["manager"]


def _status_power_cache_key(plug) -> tuple[str, str, int]:
    """Build a stable cache key for power status lookups."""
    plug_type = getattr(plug.plug_type, "value", str(plug.plug_type))
    return (plug_type, plug.address, plug.plug_index)


def _get_cached_status_power(plug, ttl: float = STATUS_POWER_CACHE_TTL) -> str | None:
    """Return a recent cached power display value if still fresh."""
    key = _status_power_cache_key(plug)
    now = time.monotonic()
    with _status_power_cache_lock:
        cached = _status_power_cache.get(key)
        if cached is None:
            return None
        cached_at, power = cached
        if now - cached_at > ttl:
            _status_power_cache.pop(key, None)
            return None
        return power


def _store_cached_status_power(plug, power: str) -> str:
    """Store a power display value and return it unchanged."""
    key = _status_power_cache_key(plug)
    with _status_power_cache_lock:
        _status_power_cache[key] = (time.monotonic(), power)
    return power


def _probe_status_power(plug, reset: str) -> str:
    """Fetch one power state, using a short cache to limit repeat probes."""
    cached = _get_cached_status_power(plug)
    if cached is not None:
        return cached

    try:
        controller = PowerController.from_plug(plug)
        state = controller.get_state()
        if state == PowerState.ON:
            power = f"\033[32mON{reset}"
        elif state == PowerState.OFF:
            power = f"\033[31mOFF{reset}"
        else:
            power = "?"
    except Exception:
        power = "err"

    return _store_cached_status_power(plug, power)


def _collect_status_power_states(sbcs, reset: str) -> dict[str, str]:
    """Resolve power states for all SBCs concurrently."""
    power_by_sbc = {sbc.name: "-" for sbc in sbcs}
    power_targets = [(sbc.name, sbc.power_plug) for sbc in sbcs if sbc.power_plug]
    if not power_targets:
        return power_by_sbc

    max_workers = min(len(power_targets), 16)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_probe_status_power, plug, reset): sbc_name
            for sbc_name, plug in power_targets
        }
        for future in concurrent.futures.as_completed(future_map):
            power_by_sbc[future_map[future]] = future.result()

    return power_by_sbc


def _sdwire_host_switch_guard_cli(
    sbc_name: str, sbc, force: bool = False, allow_force_override: bool = False
) -> str | None:
    """Refuse host-mode switching when the SBC still appears powered on."""
    if force or not sbc.power_plug:
        return None

    try:
        from labctl.power import PowerController
        from labctl.power.base import PowerState

        power_ctrl = PowerController.from_plug(sbc.power_plug)
        state = power_ctrl.get_state()
        if state == PowerState.ON:
            message = (
                f"Error: {sbc_name} is powered on. Power off before "
                f"switching SD to host mode."
            )
            if allow_force_override:
                message += "\nUse --force to override (risks SD card corruption)."
            return message
    except Exception:
        return None

    return None


@click.group(cls=AliasedGroup)
@click.version_option(version=__version__, prog_name="labctl")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-essential output")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file",
)
@click.option(
    "--delay",
    "-d",
    type=float,
    default=0,
    help="Delay in seconds before executing the command",
)
@click.pass_context
def main(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    config_path: Path | None,
    delay: float,
) -> None:
    """Lab Controller - Manage embedded development lab resources.

    \b
    Aliases:
      ls     -> list
      rm     -> remove
      delete -> remove
      show   -> info
      on     -> power on
      off    -> power off
    """
    import getpass

    from labctl.core import audit

    audit.set_context(actor=f"cli:{getpass.getuser()}", source="cli")

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["config"] = load_config(config_path)

    # Initialize logging
    config = ctx.obj["config"]
    log_level = config.log_level.upper()
    if verbose:
        log_level = "DEBUG"
    elif quiet:
        log_level = "WARNING"
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    if delay > 0:
        import time

        if not quiet:
            click.echo(f"Waiting {delay}s...")
        time.sleep(delay)


@main.command("ports")
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all serial devices, not just /dev/lab/",
)
@click.pass_context
def ports_cmd(ctx: click.Context, show_all: bool) -> None:
    """List available serial ports."""
    verbose = ctx.obj.get("verbose", False)
    config: Config = ctx.obj["config"]
    dev_dir = config.serial.dev_dir

    # Get /dev/lab/ symlinks
    lab_ports = []
    if dev_dir.exists():
        for entry in sorted(dev_dir.iterdir()):
            if entry.is_symlink():
                target = os.readlink(entry)
                # Resolve relative symlinks
                if not target.startswith("/"):
                    target = str((entry.parent / target).resolve())
                lab_ports.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "target": target,
                        "tcp_port": _get_tcp_port(entry.name, config),
                    }
                )

    if not lab_ports:
        click.echo(f"No ports configured in {dev_dir}/")
        if verbose:
            click.echo("Run discover-usb-serial.sh to find connected devices")
        return

    # Print table header
    click.echo(f"{'NAME':<20} {'DEVICE':<15} {'TCP PORT':<10}")
    click.echo("-" * 45)

    for port in lab_ports:
        tcp = port["tcp_port"] or "-"
        click.echo(f"{port['name']:<20} {port['target']:<15} {tcp:<10}")

    if verbose:
        click.echo(f"\n{len(lab_ports)} port(s) configured")


def _get_tcp_port(port_name: str, config: Config) -> int | None:
    """Look up TCP port for a named port from ser2net config."""
    ser2net_config = config.ser2net.config_file
    if not ser2net_config.exists():
        return None

    try:
        content = ser2net_config.read_text()
        # Simple parsing: look for connection name and extract port
        # Format: connection: &name ... accepter: tcp,localhost,PORT
        in_connection = False
        for line in content.splitlines():
            if f"connection: &{port_name}" in line:
                in_connection = True
            elif in_connection and "accepter:" in line:
                # Extract port number from "tcp,localhost,4001" or "tcp,4001"
                parts = line.split(",")
                for part in parts:
                    part = part.strip()
                    if part.isdigit():
                        return int(part)
                in_connection = False
            elif in_connection and line.startswith("connection:"):
                in_connection = False
    except Exception:
        pass

    return None


@main.command("connect")
@click.argument("port_name")
@click.option("--baud", "-b", type=int, help="Baud rate (ignored for TCP)")
@click.pass_context
def connect_cmd(ctx: click.Context, port_name: str, baud: int | None) -> None:
    """Connect to a serial port console.

    PORT_NAME can be a port alias, SBC name, device name (e.g., 'port-1'),
    or full path (e.g., '/dev/lab/port-1').
    """
    verbose = ctx.obj.get("verbose", False)
    config: Config = ctx.obj["config"]
    dev_dir = config.serial.dev_dir
    manager = _get_manager(ctx)

    # 1. Try alias lookup from database
    port = manager.get_serial_port_by_alias(port_name)
    if port and port.tcp_port:
        if verbose:
            click.echo(
                f"Connecting to alias '{port_name}' via TCP port {port.tcp_port}..."
            )
        _connect_tcp("localhost", port.tcp_port)
        return

    # 2. Try SBC name lookup (use console port)
    sbc = manager.get_sbc_by_name(port_name)
    if sbc and sbc.console_port and sbc.console_port.tcp_port:
        if verbose:
            click.echo(
                f"Connecting to {port_name} console via TCP port "
                f"{sbc.console_port.tcp_port}..."
            )
        _connect_tcp("localhost", sbc.console_port.tcp_port)
        return

    # 3. Fall back to filesystem path resolution
    if port_name.startswith("/"):
        port_path = Path(port_name)
        port_name = port_path.name
    else:
        port_path = dev_dir / port_name

    if not port_path.exists():
        click.echo(f"Error: Port not found: {port_path}", err=True)
        click.echo("Tip: Use a port alias, SBC name, or device path.")
        sys.exit(1)

    # Look up TCP port from ser2net config
    tcp_port = _get_tcp_port(port_name, config)

    if tcp_port and config.ser2net.enabled:
        if verbose:
            click.echo(f"Connecting to {port_name} via TCP port {tcp_port}...")
        _connect_tcp("localhost", tcp_port)
    else:
        if verbose:
            click.echo(f"Connecting directly to {port_path}...")
        _connect_direct(port_path, baud or config.serial.default_baud)


def _connect_tcp(host: str, port: int) -> None:
    """Connect to serial port via TCP using nc or telnet."""
    click.echo(f"Connecting to {host}:{port}...")
    click.echo("Press Ctrl+] then 'q' to disconnect (nc) or Ctrl+C to exit")
    click.echo("-" * 40)

    # Try nc first, then telnet
    try:
        subprocess.run(["nc", host, str(port)], check=False)
    except FileNotFoundError:
        try:
            subprocess.run(["telnet", host, str(port)], check=False)
        except FileNotFoundError:
            click.echo("Error: Neither 'nc' nor 'telnet' found", err=True)
            click.echo("Install with: sudo apt install netcat-openbsd", err=True)
            sys.exit(1)


def _connect_direct(port_path: Path, baud: int) -> None:
    """Connect directly to serial port using picocom or minicom."""
    click.echo(f"Connecting to {port_path} at {baud} baud...")
    click.echo("Press Ctrl+A then Ctrl+X to exit (picocom)")
    click.echo("-" * 40)

    # Try picocom first, then minicom
    try:
        subprocess.run(
            ["picocom", "-b", str(baud), str(port_path)],
            check=False,
        )
    except FileNotFoundError:
        try:
            subprocess.run(
                ["minicom", "-b", str(baud), "-D", str(port_path)],
                check=False,
            )
        except FileNotFoundError:
            click.echo("Error: Neither 'picocom' nor 'minicom' found", err=True)
            click.echo("Install with: sudo apt install picocom", err=True)
            sys.exit(1)


# --- SBC Management Commands ---


@main.command("list")
@click.option("--project", "-p", help="Filter by project name")
@click.option(
    "--status",
    "-s",
    type=click.Choice([s.value for s in Status]),
    help="Filter by status",
)
@click.pass_context
def list_cmd(ctx: click.Context, project: str | None, status: str | None) -> None:
    """List all SBCs."""
    manager = _get_manager(ctx)
    status_filter = Status(status) if status else None

    sbcs = manager.list_sbcs(project=project, status=status_filter)

    if not sbcs:
        click.echo("No SBCs configured. Use 'labctl add <name>' to add one.")
        return

    # Print table
    click.echo(
        f"{'NAME':<15} {'PROJECT':<12} {'STATUS':<10} {'CONSOLE':<20} {'IP':<15}"
    )
    click.echo("-" * 72)

    for sbc in sbcs:
        console = "-"
        if sbc.console_port:
            tcp = sbc.console_port.tcp_port
            console = f"tcp:{tcp}" if tcp else sbc.console_port.device_path

        ip = sbc.primary_ip or "-"
        project_name = sbc.project or "-"

        line = f"{sbc.name:<15} {project_name:<12} {sbc.status.value:<10} "
        line += f"{console:<20} {ip:<15}"
        click.echo(line)


@main.command("add")
@click.argument("name")
@click.option("--project", "-p", help="Project name")
@click.option("--description", "-d", help="Description")
@click.option("--ssh-user", "-u", default="root", help="SSH username (default: root)")
@click.pass_context
def add_cmd(
    ctx: click.Context,
    name: str,
    project: str | None,
    description: str | None,
    ssh_user: str,
) -> None:
    """Add a new SBC."""
    manager = _get_manager(ctx)

    try:
        sbc = manager.create_sbc(
            name=name,
            project=project,
            description=description,
            ssh_user=ssh_user,
        )
        click.echo(f"Added SBC: {sbc.name}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option(
    "--force",
    is_flag=True,
    help="Remove even if an active claim is held (cascades claim rows)",
)
@click.pass_context
def remove_cmd(ctx: click.Context, name: str, yes: bool, force: bool) -> None:
    """Remove an SBC."""
    from labctl.core.models import ClaimConflict

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        click.echo(f"Error: SBC '{name}' not found", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove SBC '{name}' and all associated data?", abort=True)

    try:
        deleted = manager.delete_sbc(sbc.id, force=force)
    except ClaimConflict as exc:
        holder = exc.claim.agent_name or "unknown agent"
        click.echo(
            f"Error: '{name}' is claimed by '{holder}'. "
            f"Release the claim or rerun with --force.",
            err=True,
        )
        sys.exit(1)
    if deleted:
        click.echo(f"Removed SBC: {name}")
    else:
        click.echo(f"Error: Failed to remove SBC '{name}'", err=True)
        sys.exit(1)


@main.command("info")
@click.argument("name")
@click.pass_context
def info_cmd(ctx: click.Context, name: str) -> None:
    """Show detailed information about an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        click.echo(f"Error: SBC '{name}' not found", err=True)
        sys.exit(1)

    click.echo(f"Name:        {sbc.name}")
    click.echo(f"Project:     {sbc.project or '-'}")
    click.echo(f"Description: {sbc.description or '-'}")
    click.echo(f"SSH User:    {sbc.ssh_user}")
    click.echo(f"Status:      {sbc.status.value}")
    click.echo()

    # Serial ports
    click.echo("Serial Ports:")
    if sbc.serial_ports:
        for port in sbc.serial_ports:
            tcp = f" (tcp:{port.tcp_port})" if port.tcp_port else ""
            alias = f" [{port.alias}]" if port.alias else ""
            click.echo(
                f"  {port.port_type.value}: {port.device_path}{tcp} @ {port.baud_rate}{alias}"
            )
    else:
        click.echo("  (none)")

    # Network addresses
    click.echo("\nNetwork Addresses:")
    if sbc.network_addresses:
        for addr in sbc.network_addresses:
            mac = f" ({addr.mac_address})" if addr.mac_address else ""
            click.echo(f"  {addr.address_type.value}: {addr.ip_address}{mac}")
    else:
        click.echo("  (none)")

    # Power plug
    click.echo("\nPower Plug:")
    if sbc.power_plug:
        plug = sbc.power_plug
        idx = f"[{plug.plug_index}]" if plug.plug_index > 1 else ""
        click.echo(f"  {plug.plug_type.value}: {plug.address}{idx}")
    else:
        click.echo("  (none)")

    # SDWire
    click.echo("\nSDWire:")
    if sbc.sdwire:
        click.echo(
            f"  {sbc.sdwire.name}: {sbc.sdwire.serial_number} ({sbc.sdwire.device_type})"
        )
    else:
        click.echo("  (none)")


@main.command("edit")
@click.argument("name")
@click.option("--rename", "-n", "new_name", help="Rename the SBC")
@click.option("--project", "-p", help="Set project name")
@click.option("--description", "-d", help="Set description")
@click.option("--ssh-user", "-u", help="Set SSH username")
@click.option(
    "--status", "-s", type=click.Choice([s.value for s in Status]), help="Set status"
)
@click.pass_context
def edit_cmd(
    ctx: click.Context,
    name: str,
    new_name: str | None,
    project: str | None,
    description: str | None,
    ssh_user: str | None,
    status: str | None,
) -> None:
    """Edit an SBC's properties."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        click.echo(f"Error: SBC '{name}' not found", err=True)
        sys.exit(1)

    # Check if any changes requested
    if all(v is None for v in [new_name, project, description, ssh_user, status]):
        click.echo(
            "No changes specified. "
            "Use --rename, --project, --description, --ssh-user, or --status."
        )
        return

    status_enum = Status(status) if status else None

    manager.update_sbc(
        sbc.id,
        name=new_name,
        project=project,
        description=description,
        ssh_user=ssh_user,
        status=status_enum,
    )
    if new_name:
        click.echo(f"Renamed SBC: {name} -> {new_name}")
    else:
        click.echo(f"Updated SBC: {name}")


# --- Port Assignment Commands ---


@main.group("sdwire")
def sdwire_group() -> None:
    """Manage SDWire SD card multiplexers."""
    pass


@sdwire_group.command("discover")
@click.pass_context
def sdwire_discover_cmd(ctx: click.Context) -> None:
    """Discover connected SDWire devices."""
    from labctl.sdwire.controller import discover_sdwire_devices

    manager = _get_manager(ctx)
    known = {d.serial_number: d for d in manager.list_sdwire_devices()}

    try:
        devices = discover_sdwire_devices()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not devices:
        click.echo("No SDWire devices found.")
        return

    click.echo(f"{'SERIAL':<25} {'TYPE':<10} {'BLOCK DEV':<12} {'REGISTERED'}")
    click.echo("-" * 60)

    for d in devices:
        reg = known.get(d["serial_number"])
        reg_str = reg.name if reg else "-"
        block = d.get("block_dev") or "-"
        click.echo(
            f"{d['serial_number']:<25} {d['device_type']:<10} " f"{block:<12} {reg_str}"
        )


@sdwire_group.command("add")
@click.argument("name")
@click.argument("serial_number")
@click.option(
    "--type",
    "device_type",
    type=click.Choice(["sdwire", "sdwirec"]),
    default=None,
    help="Device type (auto-detected if not specified)",
)
@click.pass_context
def sdwire_add_cmd(
    ctx: click.Context, name: str, serial_number: str, device_type: str | None
) -> None:
    """Register an SDWire device.

    NAME is a short identifier (e.g., sdwire-1).
    SERIAL_NUMBER is the USB serial (from 'labctl sdwire discover').
    """
    manager = _get_manager(ctx)

    # Auto-detect type from connected devices if not specified
    if device_type is None:
        try:
            from labctl.sdwire.controller import discover_sdwire_devices

            for d in discover_sdwire_devices():
                if d["serial_number"] == serial_number:
                    device_type = d["device_type"]
                    break
        except RuntimeError:
            pass
        if device_type is None:
            device_type = "sdwirec"

    try:
        device = manager.create_sdwire_device(
            name=name,
            serial_number=serial_number,
            device_type=device_type,
        )
        click.echo(
            f"Registered SDWire device: {device.name} ({device.serial_number}) "
            f"type={device.device_type}"
        )
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def sdwire_remove_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Unregister an SDWire device."""
    manager = _get_manager(ctx)

    device = manager.get_sdwire_device_by_name(name)
    if not device:
        click.echo(f"Error: SDWire device '{name}' not found", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove SDWire device '{name}'?", abort=True)

    try:
        manager.delete_sdwire_device(device.id)
        click.echo(f"Removed SDWire device: {name}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("list")
@click.pass_context
def sdwire_list_cmd(ctx: click.Context) -> None:
    """List all registered SDWire devices."""
    manager = _get_manager(ctx)

    devices = manager.list_sdwire_devices()
    if not devices:
        click.echo(
            "No SDWire devices registered. Use 'labctl sdwire discover' to find devices."
        )
        return

    # Check assignments
    sbcs = manager.list_sbcs()
    assigned = {}
    for sbc in sbcs:
        if sbc.sdwire:
            assigned[sbc.sdwire.id] = sbc.name

    click.echo(f"{'NAME':<15} {'SERIAL':<25} {'TYPE':<10} {'ASSIGNED TO'}")
    click.echo("-" * 60)

    for d in devices:
        sbc_name = assigned.get(d.id, "-")
        click.echo(f"{d.name:<15} {d.serial_number:<25} {d.device_type:<10} {sbc_name}")


@sdwire_group.command("assign")
@click.argument("sbc_name")
@click.argument("device_name")
@click.pass_context
def sdwire_assign_cmd(ctx: click.Context, sbc_name: str, device_name: str) -> None:
    """Assign an SDWire device to an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    device = manager.get_sdwire_device_by_name(device_name)
    if not device:
        click.echo(f"Error: SDWire device '{device_name}' not found", err=True)
        sys.exit(1)

    try:
        manager.assign_sdwire(sbc.id, device.id)
        click.echo(f"Assigned SDWire '{device_name}' to {sbc_name}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("unassign")
@click.argument("sbc_name")
@click.pass_context
def sdwire_unassign_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Remove SDWire assignment from an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    if manager.unassign_sdwire(sbc.id):
        click.echo(f"Removed SDWire assignment from {sbc_name}")
    else:
        click.echo(f"No SDWire assigned to {sbc_name}")


@sdwire_group.command("dut")
@click.argument("sbc_name")
@click.pass_context
def sdwire_dut_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Switch SD card to DUT (SBC boots from SD)."""
    from labctl.sdwire.controller import SDWireController

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_dut()
        click.echo(f"SD card switched to DUT: {sbc_name}")
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("host")
@click.argument("sbc_name")
@click.option("--force", is_flag=True, help="Override power-on safety check")
@click.pass_context
def sdwire_host_cmd(ctx: click.Context, sbc_name: str, force: bool) -> None:
    """Switch SD card to host (dev machine can flash it).

    Refuses if the SBC is powered on to prevent SD card bus contention.
    Use --force to override (e.g., if SBC is halted but power relay is on).
    """
    from labctl.sdwire.controller import SDWireController

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)

    guard_err = _sdwire_host_switch_guard_cli(
        sbc_name, sbc, force=force, allow_force_override=True
    )
    if guard_err:
        click.echo(guard_err, err=True)
        sys.exit(1)

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_host()
        block_dev = ctrl.get_block_device()
        msg = f"SD card switched to host: {sbc_name}"
        if block_dev:
            msg += f" ({block_dev})"
        click.echo(msg)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("flash")
@click.argument("sbc_name")
@click.argument("image", type=click.Path(exists=True, path_type=Path))
@click.option("--no-reboot", is_flag=True, help="Don't power cycle after flashing")
@click.option(
    "--copy",
    "-c",
    "post_copies",
    multiple=True,
    help="Copy file to boot partition after flash (source:dest)",
)
@click.pass_context
def sdwire_flash_cmd(
    ctx: click.Context,
    sbc_name: str,
    image: Path,
    no_reboot: bool,
    post_copies: tuple[str, ...],
) -> None:
    """Flash an SD card image to an SBC's SDWire.

    Supports .img, .img.xz, and .img.gz formats. Switches to host,
    writes the image, optionally copies files to the boot partition,
    switches back to DUT, and optionally power cycles.

    \b
    Examples:
      labctl sdwire flash pi-5 raspios.img
      labctl sdwire flash pi-5 raspios.img.xz -c config.txt:config.txt
      labctl sdwire flash pi-5 raspios.img --no-reboot
    """
    import time

    from labctl.sdwire.controller import SDWireController

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    flash_ok = False

    try:
        # Power off (best effort)
        if sbc.power_plug:
            click.echo("Powering off SBC...")
            try:
                from labctl.power import PowerController

                power_ctrl = PowerController.from_plug(sbc.power_plug)
                power_ctrl.power_off()
                time.sleep(1)
            except Exception:
                pass

        # Switch to host
        click.echo("Switching SD card to host...")
        ctrl.switch_to_host()
        time.sleep(2)

        block_dev = ctrl.get_block_device(settle_time=2)
        if not block_dev:
            block_dev = ctrl.get_block_device(settle_time=5)
        if not block_dev:
            click.echo("Error: Block device not found (waited 10s)", err=True)
            sys.exit(1)

        # Flash image (includes safety validation)
        click.echo(f"Flashing {image} to {block_dev}...")
        result = ctrl.flash_image(str(image))
        flash_ok = True
        click.echo(
            f"Flash complete: {result['bytes_written']} bytes "
            f"in {result['elapsed_seconds']}s"
        )

        # Post-flash copies
        if post_copies:
            import subprocess

            click.echo("Re-reading partition table...")
            subprocess.run(
                ["sudo", "partprobe", block_dev],
                capture_output=True,
                timeout=10,
            )
            time.sleep(2)

            file_pairs = []
            for spec in post_copies:
                if ":" not in spec:
                    click.echo(
                        f"Warning: Invalid --copy format '{spec}', skipping", err=True
                    )
                    continue
                src, dest = spec.split(":", 1)
                file_pairs.append((src, dest))

            if file_pairs:
                click.echo("Copying files to boot partition...")
                copied = ctrl.update_files(1, file_pairs)
                for f in copied["copied"]:
                    click.echo(f"  Copied: {f}")

        # Switch back to DUT
        click.echo("Switching SD card to DUT...")
        ctrl.switch_to_dut()

        # Optional power cycle
        if not no_reboot and sbc.power_plug:
            click.echo(f"Power cycling {sbc_name}...")
            from labctl.power import PowerController

            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_cycle()
            click.echo(f"Power cycled: {sbc_name}")

        click.echo(f"Done! {sbc_name} should boot from the new image.")

    except RuntimeError as e:
        if not flash_ok:
            click.echo(f"Error: {e}", err=True)
            click.echo("SD card left on host for inspection.", err=True)
        else:
            click.echo(f"Error: {e}", err=True)
            try:
                ctrl.switch_to_dut()
            except Exception:
                pass
        sys.exit(1)


@sdwire_group.command("update")
@click.argument("sbc_name")
@click.option(
    "--partition",
    "-p",
    type=int,
    required=True,
    help="Partition number (e.g., 1 for first partition)",
)
@click.option(
    "--copy",
    "-c",
    "copies",
    multiple=True,
    help="File to copy as source:dest (dest relative to partition root)",
)
@click.option(
    "--rename",
    "-r",
    "renames",
    multiple=True,
    help="Rename file as oldname:newname (both relative to partition root)",
)
@click.option(
    "--delete",
    "-d",
    "deletes",
    multiple=True,
    help="Delete file (relative to partition root)",
)
@click.option("--reboot", is_flag=True, help="Power cycle the SBC after updating")
@click.pass_context
def sdwire_update_cmd(
    ctx: click.Context,
    sbc_name: str,
    partition: int,
    copies: tuple[str, ...],
    renames: tuple[str, ...],
    deletes: tuple[str, ...],
    reboot: bool,
) -> None:
    """Copy, rename, and/or delete files on a partition on an SBC's SD card.

    Switches to host, mounts the partition, performs operations (copies first,
    then renames, then deletes), unmounts, switches back to DUT, and optionally
    power cycles.

    \b
    Examples:
      labctl sdwire update pi-5 -p 1 --copy kernel.img:kernel_2712.img
      labctl sdwire update pi-5 -p 1 --rename armstub.bin:armstub.bin.disabled
      labctl sdwire update pi-5 -p 1 --delete old-config.txt
      labctl sdwire update pi-5 -p 1 -c fw.bin:firmware.bin -r old.bin:old.bin.bak -d stale.txt --reboot
    """
    from labctl.sdwire.controller import SDWireController

    if not copies and not renames and not deletes:
        click.echo(
            "Error: At least one --copy, --rename, or --delete is required",
            err=True,
        )
        sys.exit(1)

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)

    # Parse copy pairs
    file_pairs = []
    for copy_spec in copies:
        if ":" not in copy_spec:
            click.echo(
                f"Error: Invalid --copy format '{copy_spec}'. Use source:dest",
                err=True,
            )
            sys.exit(1)
        src, dest = copy_spec.split(":", 1)
        if not Path(src).exists():
            click.echo(f"Error: Source file not found: {src}", err=True)
            sys.exit(1)
        file_pairs.append((src, dest))

    # Parse rename pairs
    rename_pairs = []
    for rename_spec in renames:
        if ":" not in rename_spec:
            click.echo(
                f"Error: Invalid --rename format '{rename_spec}'. Use oldname:newname",
                err=True,
            )
            sys.exit(1)
        old_name, new_name = rename_spec.split(":", 1)
        rename_pairs.append((old_name, new_name))

    # Deletes are just filenames, no parsing needed
    delete_list = list(deletes)

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

    try:
        # Step 0: Auto power-off to prevent SD card bus contention
        if sbc.power_plug:
            click.echo(f"Powering off {sbc_name}...")
            try:
                from labctl.power import PowerController

                power_ctrl = PowerController.from_plug(sbc.power_plug)
                power_ctrl.power_off()
                import time as time_mod

                time_mod.sleep(1)
            except Exception:
                click.echo("Warning: Could not power off (continuing anyway)", err=True)

        # Step 1: Switch to host
        click.echo("Switching SD card to host...")
        ctrl.switch_to_host()

        import time

        time.sleep(2)  # Wait for block device and partitions to appear

        # Step 2: Mount and perform operations
        click.echo(f"Mounting partition {partition}...")
        result = ctrl.update_files(
            partition,
            file_pairs,
            renames=rename_pairs or None,
            deletes=delete_list or None,
        )
        for f in result["copied"]:
            click.echo(f"  Copied: {f}")
        for f in result["renamed"]:
            click.echo(f"  Renamed: {f}")
        for f in result["deleted"]:
            click.echo(f"  Deleted: {f}")

        # Step 3: Switch back to DUT
        click.echo("Switching SD card to DUT...")
        ctrl.switch_to_dut()

        # Step 4: Optional power cycle
        if reboot and sbc.power_plug:
            click.echo(f"Power cycling {sbc_name}...")
            from labctl.power import PowerController

            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_cycle(delay=3.0)
            click.echo(f"Power cycled: {sbc_name}")
        elif reboot and not sbc.power_plug:
            click.echo("Warning: --reboot requested but no power plug assigned")

        click.echo("Done!")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("ls")
@click.argument("sbc_name")
@click.option(
    "--partition",
    "-p",
    type=int,
    required=True,
    help="Partition number (e.g., 1 for first partition)",
)
@click.option(
    "--path",
    "target_path",
    default="/",
    show_default=True,
    help="Absolute path within the partition",
)
@click.option("--recursive", "-r", is_flag=True, help="Recurse into subdirectories")
@click.option(
    "--max-entries",
    type=int,
    default=1000,
    show_default=True,
    help="Safety cap on returned entries",
)
@click.pass_context
def sdwire_ls_cmd(
    ctx: click.Context,
    sbc_name: str,
    partition: int,
    target_path: str,
    recursive: bool,
    max_entries: int,
) -> None:
    """List directory contents on an SBC SD card partition."""
    from labctl.sdwire.controller import SDWireController

    manager = _get_manager(ctx)
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)
    guard_err = _sdwire_host_switch_guard_cli(sbc_name, sbc)
    if guard_err:
        click.echo(guard_err, err=True)
        sys.exit(1)

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    host_switched = False
    cleanup_error: RuntimeError | None = None
    output: str

    try:
        ctrl.switch_to_host()
        host_switched = True
        import time

        time.sleep(2)
        result = ctrl.list_files(
            partition=partition,
            path=target_path,
            recursive=recursive,
            max_entries=max_entries,
        )
        output = json.dumps(
            {
                "sbc_name": sbc_name,
                "partition": partition,
                "path": target_path,
                **result,
            },
            indent=2,
        )
    except FileNotFoundError:
        click.echo(f"Error: Path not found: {target_path}", err=True)
        sys.exit(1)
    except PermissionError:
        click.echo(f"Error: Permission denied: {target_path}", err=True)
        sys.exit(1)
    except NotADirectoryError:
        click.echo(f"Error: Not a directory: {target_path}", err=True)
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        click.echo(
            f"Error: Failed to restore SD card to DUT mode: {cleanup_error}",
            err=True,
        )
        sys.exit(1)
    click.echo(output)


@sdwire_group.command("cat")
@click.argument("sbc_name")
@click.option(
    "--partition",
    "-p",
    type=int,
    required=True,
    help="Partition number (e.g., 1 for first partition)",
)
@click.option("--path", "target_path", required=True, help="Absolute file path")
@click.option(
    "--max-bytes",
    type=int,
    default=1024 * 1024,
    show_default=True,
    help="Maximum bytes to read",
)
@click.option(
    "--encoding",
    type=click.Choice(["text", "base64", "hex"]),
    default="text",
    show_default=True,
    help="Output encoding",
)
@click.pass_context
def sdwire_cat_cmd(
    ctx: click.Context,
    sbc_name: str,
    partition: int,
    target_path: str,
    max_bytes: int,
    encoding: str,
) -> None:
    """Read a file from an SBC SD card partition."""
    from labctl.sdwire.controller import SDWireController, SDWireSymlinkError

    manager = _get_manager(ctx)
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)
    guard_err = _sdwire_host_switch_guard_cli(sbc_name, sbc)
    if guard_err:
        click.echo(guard_err, err=True)
        sys.exit(1)

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    host_switched = False
    cleanup_error: RuntimeError | None = None
    output: str

    try:
        ctrl.switch_to_host()
        host_switched = True
        import time

        time.sleep(2)
        result = ctrl.read_file(
            partition=partition,
            path=target_path,
            max_bytes=max_bytes,
            encoding=encoding,
        )
        output = json.dumps(
            {
                "sbc_name": sbc_name,
                "partition": partition,
                "path": target_path,
                **result,
            },
            indent=2,
        )
    except FileNotFoundError:
        click.echo(f"Error: Path not found: {target_path}", err=True)
        sys.exit(1)
    except PermissionError:
        click.echo(f"Error: Permission denied: {target_path}", err=True)
        sys.exit(1)
    except IsADirectoryError:
        click.echo(f"Error: Not a file: {target_path}", err=True)
        sys.exit(1)
    except ValueError as e:
        if str(e) == "binary_content":
            click.echo(
                f"Error: File is not valid UTF-8: {target_path}. "
                "Retry with --encoding base64.",
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except SDWireSymlinkError as e:
        click.echo(
            f"Error: Refusing to follow symlink: {target_path} -> {e.target}",
            err=True,
        )
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        click.echo(
            f"Error: Failed to restore SD card to DUT mode: {cleanup_error}",
            err=True,
        )
        sys.exit(1)
    click.echo(output)


@sdwire_group.command("info")
@click.argument("sbc_name")
@click.pass_context
def sdwire_info_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Show partition metadata for an SBC SD card."""
    from labctl.sdwire.controller import SDWireController

    manager = _get_manager(ctx)
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)
    if not sbc.sdwire:
        click.echo(f"Error: No SDWire assigned to '{sbc_name}'", err=True)
        sys.exit(1)
    guard_err = _sdwire_host_switch_guard_cli(sbc_name, sbc)
    if guard_err:
        click.echo(guard_err, err=True)
        sys.exit(1)

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    host_switched = False
    cleanup_error: RuntimeError | None = None
    output: str

    try:
        ctrl.switch_to_host()
        host_switched = True
        import time

        time.sleep(2)
        result = ctrl.get_disk_info()
        output = json.dumps({"sbc_name": sbc_name, **result}, indent=2)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        click.echo(
            f"Error: Failed to restore SD card to DUT mode: {cleanup_error}",
            err=True,
        )
        sys.exit(1)
    click.echo(output)


@main.group("serial")
def serial_group() -> None:
    """Manage USB-serial adapters."""
    pass


@serial_group.command("discover")
@click.option("--json-output", "-j", "json_out", is_flag=True, help="Output as JSON")
@click.pass_context
def serial_discover_cmd(ctx: click.Context, json_out: bool) -> None:
    """Discover connected USB-serial devices."""
    import json

    from labctl.serial.udev import discover_usb_serial

    manager = _get_manager(ctx)
    devices = discover_usb_serial()
    known = {d.usb_path: d for d in manager.list_serial_devices()}

    if json_out:
        click.echo(json.dumps(devices, indent=2))
        return

    if not devices:
        click.echo("No USB-serial devices found.")
        return

    click.echo(
        f"{'DEVICE':<12} {'USB PATH':<15} {'VENDOR':<20} {'MODEL':<25} {'REGISTERED'}"
    )
    click.echo("-" * 85)

    for d in devices:
        reg = known.get(d["usb_path"])
        reg_str = reg.name if reg else "-"
        click.echo(
            f"{d['device']:<12} {d['usb_path']:<15} "
            f"{d['vendor'][:18]:<20} {d['model'][:23]:<25} {reg_str}"
        )


@serial_group.command("add")
@click.argument("name")
@click.argument("usb_path")
@click.option("--vendor", help="Vendor name")
@click.option("--model", help="Model name")
@click.option("--serial-number", help="USB serial number")
@click.pass_context
def serial_add_cmd(
    ctx: click.Context,
    name: str,
    usb_path: str,
    vendor: str | None,
    model: str | None,
    serial_number: str | None,
) -> None:
    """Register a USB-serial adapter.

    NAME is a short identifier (e.g., port-1, cp2102-a).
    USB_PATH is the physical USB path (e.g., 1-10.1.3).
    """
    manager = _get_manager(ctx)

    try:
        device = manager.create_serial_device(
            name=name,
            usb_path=usb_path,
            vendor=vendor,
            model=model,
            serial_number=serial_number,
        )
        click.echo(f"Registered serial device: {device.name} ({device.usb_path})")
        click.echo(f"Udev symlink: /dev/lab/{device.name}")
        click.echo("Run 'labctl serial udev --install' to activate udev rules.")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@serial_group.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def serial_remove_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Unregister a USB-serial adapter."""
    manager = _get_manager(ctx)

    device = manager.get_serial_device_by_name(name)
    if not device:
        click.echo(f"Error: Serial device '{name}' not found", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove serial device '{name}'?", abort=True)

    try:
        manager.delete_serial_device(device.id)
        click.echo(f"Removed serial device: {name}")
        click.echo("Run 'labctl serial udev --install' to update udev rules.")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@serial_group.command("list")
@click.pass_context
def serial_list_cmd(ctx: click.Context) -> None:
    """List all registered USB-serial adapters."""
    manager = _get_manager(ctx)

    devices = manager.list_serial_devices()
    if not devices:
        click.echo(
            "No serial devices registered. Use 'labctl serial discover' to find devices."
        )
        return

    # Check which devices are in use
    ports = manager.list_serial_ports()
    in_use = {}
    sbc_names = {s.id: s.name for s in manager.list_sbcs()}
    for p in ports:
        if p.serial_device_id:
            sbc_name = sbc_names.get(p.sbc_id, "?")
            alias = p.alias or ""
            in_use[p.serial_device_id] = f"{sbc_name} ({alias})" if alias else sbc_name

    click.echo(
        f"{'NAME':<15} {'USB PATH':<15} {'VENDOR':<18} {'MODEL':<20} {'ASSIGNED TO'}"
    )
    click.echo("-" * 80)

    for d in devices:
        assigned = in_use.get(d.id, "-")
        vendor = (d.vendor or "-")[:16]
        model = (d.model or "-")[:18]
        click.echo(f"{d.name:<15} {d.usb_path:<15} {vendor:<18} {model:<20} {assigned}")


@serial_group.command("repair")
@click.option(
    "--apply",
    is_flag=True,
    help="Write the repair. Without this flag, prints the plan only.",
)
@click.pass_context
def serial_repair_cmd(ctx: click.Context, apply: bool) -> None:
    """Backfill missing serial_device_id links on serial_ports rows.

    When a port is assigned by device-path alone (CLI without
    `--serial-device`, or by an older MCP tool), the FK to
    `serial_devices` may be left NULL. The port still works, but
    `labctl serial list` shows the adapter as unassigned.

    This command scans for NULL-FK rows and repairs them by matching
    `device_path` against registered adapter names. Dry-run by default.
    """
    manager = _get_manager(ctx)
    results = manager.repair_serial_port_links(apply=apply)

    if not results:
        click.echo("No orphan serial-port links found.")
        return

    click.echo(
        f"{'PORT':<6} {'SBC_ID':<7} {'ALIAS':<26} {'DEVICE_PATH':<24} "
        f"{'RESOLVED':<14} STATUS"
    )
    click.echo("-" * 95)
    for e in results:
        resolved = e["resolved_name"] or "-"
        click.echo(
            f"{e['port_id']:<6} {e['sbc_id']:<7} "
            f"{(e['alias'] or '-'):<26} {(e['device_path'] or '-'):<24} "
            f"{resolved:<14} {e['status']}"
        )

    applied = sum(1 for e in results if e["status"] == "applied")
    repaired = sum(1 for e in results if e["status"] == "repaired")
    unresolved = sum(1 for e in results if e["status"] == "unresolvable")

    click.echo()
    if apply:
        click.echo(f"Applied: {applied}.  Unresolvable: {unresolved}.")
    else:
        click.echo(
            f"Would repair: {repaired}.  Unresolvable: {unresolved}.  "
            f"Re-run with --apply to write."
        )


@serial_group.command("rename")
@click.argument("name")
@click.argument("new_name")
@click.pass_context
def serial_rename_cmd(ctx: click.Context, name: str, new_name: str) -> None:
    """Rename a USB-serial adapter."""
    manager = _get_manager(ctx)

    device = manager.get_serial_device_by_name(name)
    if not device:
        click.echo(f"Error: Serial device '{name}' not found", err=True)
        sys.exit(1)

    try:
        manager.rename_serial_device(device.id, new_name)
        click.echo(f"Renamed: {name} -> {new_name}")
        click.echo("Run 'labctl serial udev --install' to update udev rules.")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@serial_group.command("udev")
@click.option("--install", is_flag=True, help="Install rules to /etc/udev/rules.d/")
@click.option("--reload", is_flag=True, help="Reload udev rules after install")
@click.pass_context
def serial_udev_cmd(ctx: click.Context, install: bool, reload: bool) -> None:
    """Generate udev rules from registered serial devices."""
    from labctl.serial.udev import (
        generate_udev_rules,
        install_udev_rules,
        reload_udev,
    )

    manager = _get_manager(ctx)
    devices = manager.list_serial_devices()

    if not devices:
        click.echo("No serial devices registered. Nothing to generate.")
        return

    rules = generate_udev_rules(devices)

    if install:
        try:
            install_udev_rules(rules)
            click.echo(f"Installed udev rules ({len(devices)} devices)")
        except PermissionError:
            click.echo("Error: Permission denied. Try with sudo.", err=True)
            sys.exit(1)

        if reload:
            if reload_udev():
                click.echo("Udev rules reloaded")
            else:
                click.echo("Warning: Failed to reload udev rules", err=True)
    else:
        click.echo(rules)


@serial_group.command("capture")
@click.argument("port_name")
@click.option(
    "--timeout",
    "-t",
    type=float,
    default=15.0,
    help="Max seconds to capture (default: 15)",
)
@click.option(
    "--until",
    "-u",
    "until_pattern",
    help="Stop when this regex pattern matches a line",
)
@click.option(
    "--tail",
    "-n",
    type=int,
    help="Return only the last N lines",
)
@click.pass_context
def serial_capture_cmd(
    ctx: click.Context,
    port_name: str,
    timeout: float,
    until_pattern: str | None,
    tail: int | None,
) -> None:
    """Capture serial output from a port.

    PORT_NAME is a port alias or SBC name (defaults to console port).
    Captures output until timeout or pattern match.

    \b
    Examples:
      labctl serial capture pi-5-1-console --timeout 15
      labctl serial capture pi-5-1 --until "slmos>" --timeout 30
      labctl serial capture pi-5-1 --until "slmos>" --tail 20
    """
    from labctl.serial.capture import capture_serial_output, resolve_port

    manager = _get_manager(ctx)

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not port.tcp_port:
        click.echo("Error: Port has no TCP port configured", err=True)
        sys.exit(1)

    try:
        result = capture_serial_output(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            timeout=timeout,
            until_pattern=until_pattern,
            tail=tail,
        )

        if result.output:
            click.echo(result.output)

        status = f"pattern matched" if result.pattern_matched else "timeout"
        click.echo(
            f"\n[{result.lines} lines, {result.elapsed_seconds:.1f}s, {status}]",
            err=True,
        )

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@serial_group.command("send")
@click.argument("port_name")
@click.argument("data")
@click.option(
    "--raw",
    is_flag=True,
    help="Send raw data without appending newline",
)
@click.option(
    "--capture",
    "-c",
    "capture_timeout",
    type=float,
    help="Capture response for N seconds after sending",
)
@click.option(
    "--until",
    "-u",
    "capture_until",
    help="Stop capture when this regex pattern matches",
)
@click.pass_context
def serial_send_cmd(
    ctx: click.Context,
    port_name: str,
    data: str,
    raw: bool,
    capture_timeout: float | None,
    capture_until: str | None,
) -> None:
    """Send data to a serial port.

    PORT_NAME is a port alias or SBC name (defaults to console port).

    \b
    Examples:
      labctl serial send pi-5-1-console "help"
      labctl serial send pi-5-1 --raw "ABCD\\r\\n"
      labctl serial send pi-5-1 "help" --capture 5
      labctl serial send pi-5-1 "help" --capture 5 --until ">"
    """
    from labctl.serial.capture import resolve_port, send_serial_data

    manager = _get_manager(ctx)

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not port.tcp_port:
        click.echo("Error: Port has no TCP port configured", err=True)
        sys.exit(1)

    try:
        result = send_serial_data(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            data=data,
            newline=not raw,
            capture_timeout=capture_timeout,
            capture_until=capture_until,
        )

        if result.capture and result.capture.output:
            click.echo(result.capture.output)
            status = "pattern matched" if result.capture.pattern_matched else "timeout"
            click.echo(
                f"\n[{result.capture.lines} lines, "
                f"{result.capture.elapsed_seconds:.1f}s, {status}]",
                err=True,
            )
        else:
            click.echo(f"Sent {result.bytes_sent} bytes")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.group("port")
def port_group() -> None:
    """Manage serial port assignments."""
    pass


@port_group.command("assign")
@click.argument("sbc_name")
@click.argument("port_type", type=click.Choice([t.value for t in PortType]))
@click.argument("device", required=False, default=None)
@click.option(
    "--tcp-port", "-t", type=int, help="TCP port (auto-assigned if not specified)"
)
@click.option(
    "--baud", "-b", type=int, default=115200, help="Baud rate (default: 115200)"
)
@click.option("--alias", "-a", help="Human-friendly name for this assignment")
@click.option(
    "--serial-device",
    "-s",
    "serial_device_name",
    help="Name of a registered serial device (auto-sets device path)",
)
@click.pass_context
def port_assign_cmd(
    ctx: click.Context,
    sbc_name: str,
    port_type: str,
    device: str | None,
    tcp_port: int | None,
    baud: int,
    alias: str | None,
    serial_device_name: str | None,
) -> None:
    """Assign a serial port to an SBC.

    DEVICE is the device path (e.g. /dev/lab/port-1). If --serial-device is
    given, the device path is derived automatically.
    """
    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    serial_device_id = None
    if serial_device_name:
        sd = manager.get_serial_device_by_name(serial_device_name)
        if not sd:
            click.echo(
                f"Error: Serial device '{serial_device_name}' not found", err=True
            )
            sys.exit(1)
        serial_device_id = sd.id
        if not device:
            device = str(config.serial.dev_dir / sd.name)

    if not device:
        click.echo(
            "Error: DEVICE argument is required (or use --serial-device)", err=True
        )
        sys.exit(1)

    try:
        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType(port_type),
            device_path=device,
            tcp_port=tcp_port,
            baud_rate=baud,
            alias=alias,
            serial_device_id=serial_device_id,
        )
        alias_str = f" as '{alias}'" if alias else ""
        click.echo(
            f"Assigned {port_type} port to {sbc_name}{alias_str}: {device} (tcp:{port.tcp_port})"
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@port_group.command("remove")
@click.argument("sbc_name")
@click.argument("port_type", type=click.Choice([t.value for t in PortType]))
@click.pass_context
def port_remove_cmd(ctx: click.Context, sbc_name: str, port_type: str) -> None:
    """Remove a serial port assignment."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    if manager.remove_serial_port(sbc.id, PortType(port_type)):
        click.echo(f"Removed {port_type} port from {sbc_name}")
    else:
        click.echo(f"No {port_type} port assigned to {sbc_name}")


@port_group.command("list")
@click.option(
    "--unassigned",
    "-u",
    is_flag=True,
    help="Also show unassigned /dev/lab/* devices",
)
@click.pass_context
def port_list_cmd(ctx: click.Context, unassigned: bool) -> None:
    """List all serial port assignments."""
    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    ports = manager.list_serial_ports()

    # Get SBC names for display
    sbc_names = {}
    for sbc in manager.list_sbcs():
        sbc_names[sbc.id] = sbc.name

    # Get assigned device paths
    assigned_devices = {p.device_path for p in ports}

    if ports:
        click.echo(
            f"{'SBC':<15} {'TYPE':<10} {'ALIAS':<18} {'DEVICE':<25} {'TCP':<8} {'BAUD':<10}"
        )
        click.echo("-" * 86)

        for port in ports:
            sbc_name = sbc_names.get(port.sbc_id, f"#{port.sbc_id}")
            tcp = str(port.tcp_port) if port.tcp_port else "-"
            alias = port.alias or "-"
            line = f"{sbc_name:<15} {port.port_type.value:<10} {alias:<18} "
            line += f"{port.device_path:<25} {tcp:<8} {port.baud_rate:<10}"
            click.echo(line)
    else:
        click.echo(
            "No serial ports assigned. Use 'labctl port assign' to assign ports."
        )

    # Show unassigned devices
    if unassigned:
        dev_dir = config.serial.dev_dir
        unassigned_devices = []

        if dev_dir.exists():
            for entry in sorted(dev_dir.iterdir()):
                if entry.is_symlink():
                    device_path = str(entry)
                    if device_path not in assigned_devices:
                        # Get target for display
                        target = os.readlink(entry)
                        if not target.startswith("/"):
                            target = str((entry.parent / target).resolve())
                        unassigned_devices.append(
                            {"name": entry.name, "path": device_path, "target": target}
                        )

        if unassigned_devices:
            click.echo(f"\nUnassigned devices in {dev_dir}/:")
            click.echo(f"{'NAME':<25} {'TARGET':<20}")
            click.echo("-" * 45)
            for dev in unassigned_devices:
                click.echo(f"{dev['name']:<25} {dev['target']:<20}")
        else:
            click.echo(f"\nNo unassigned devices in {dev_dir}/")


# --- Network Address Commands ---


@main.group("network")
def network_group() -> None:
    """Manage network address assignments."""
    pass


@network_group.command("set")
@click.argument("sbc_name")
@click.argument("address_type", type=click.Choice([t.value for t in AddressType]))
@click.argument("ip_address")
@click.option("--mac", "-m", help="MAC address")
@click.option("--hostname", "-h", help="Hostname")
@click.pass_context
def network_set_cmd(
    ctx: click.Context,
    sbc_name: str,
    address_type: str,
    ip_address: str,
    mac: str | None,
    hostname: str | None,
) -> None:
    """Set a network address for an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    manager.set_network_address(
        sbc_id=sbc.id,
        address_type=AddressType(address_type),
        ip_address=ip_address,
        mac_address=mac,
        hostname=hostname,
    )
    click.echo(f"Set {address_type} address for {sbc_name}: {ip_address}")


@network_group.command("remove")
@click.argument("sbc_name")
@click.argument("address_type", type=click.Choice([t.value for t in AddressType]))
@click.pass_context
def network_remove_cmd(ctx: click.Context, sbc_name: str, address_type: str) -> None:
    """Remove a network address from an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    if manager.remove_network_address(sbc.id, AddressType(address_type)):
        click.echo(f"Removed {address_type} address from {sbc_name}")
    else:
        click.echo(f"No {address_type} address assigned to {sbc_name}")


# --- ser2net Commands ---


@main.group("ser2net")
def ser2net_group() -> None:
    """Manage ser2net configuration."""
    pass


@ser2net_group.command("generate")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: stdout)",
)
@click.option("--install", is_flag=True, help="Install to /etc/ser2net.yaml")
@click.pass_context
def ser2net_generate_cmd(
    ctx: click.Context, output: Path | None, install: bool
) -> None:
    """Generate ser2net configuration from database."""
    manager = _get_manager(ctx)

    # Get all serial ports from database
    db_ports = manager.list_serial_ports()

    if not db_ports:
        click.echo("No serial ports configured. Use 'labctl port assign' first.")
        return

    # Get SBC names for connection naming
    sbc_names = {}
    for sbc in manager.list_sbcs():
        sbc_names[sbc.id] = sbc.name

    # Convert to Ser2NetPort objects
    ports = []
    for db_port in db_ports:
        sbc_name = sbc_names.get(db_port.sbc_id, f"sbc{db_port.sbc_id}")
        port_name = db_port.alias or f"{sbc_name}-{db_port.port_type.value}"

        ports.append(
            Ser2NetPort(
                name=port_name,
                device=db_port.device_path,
                tcp_port=db_port.tcp_port or 4000,
                baud=db_port.baud_rate,
            )
        )

    # Generate config
    config_content = generate_ser2net_config(ports)

    if install:
        # Write to /etc/ser2net.yaml
        ser2net_path = Path("/etc/ser2net.yaml")
        try:
            ser2net_path.write_text(config_content)
            click.echo(f"Installed ser2net config to {ser2net_path}")
            click.echo("Run 'labctl ser2net reload' to apply changes")
        except PermissionError:
            click.echo("Error: Permission denied. Try with sudo.", err=True)
            sys.exit(1)
    elif output:
        output.write_text(config_content)
        click.echo(f"Wrote ser2net config to {output}")
    else:
        click.echo(config_content)


@ser2net_group.command("reload")
@click.pass_context
def ser2net_reload_cmd(ctx: click.Context) -> None:
    """Reload ser2net service."""
    for use_sudo in (False, True):
        try:
            prefix = ["sudo"] if use_sudo else []
            result = subprocess.run(
                [*prefix, "systemctl", "restart", "ser2net"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                click.echo("ser2net service restarted successfully")
                return
            elif not use_sudo and "Permission" in (result.stderr or ""):
                continue  # Retry with sudo
            else:
                click.echo(f"Error restarting ser2net: {result.stderr}", err=True)
                sys.exit(1)
        except FileNotFoundError:
            click.echo("Error: systemctl not found", err=True)
            sys.exit(1)


# --- Plug Assignment Commands ---


@main.group("plug")
def plug_group() -> None:
    """Manage power plug assignments."""
    pass


@plug_group.command("assign")
@click.argument("sbc_name")
@click.argument("plug_type", type=click.Choice([t.value for t in PlugType]))
@click.argument("address")
@click.option(
    "--index",
    "-i",
    type=int,
    default=1,
    help="Outlet index for multi-relay devices (default: 1)",
)
@click.pass_context
def plug_assign_cmd(
    ctx: click.Context,
    sbc_name: str,
    plug_type: str,
    address: str,
    index: int,
) -> None:
    """Assign a power plug to an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    manager.assign_power_plug(
        sbc_id=sbc.id,
        plug_type=PlugType(plug_type),
        address=address,
        plug_index=index,
    )
    idx_str = f"[{index}]" if index > 1 else ""
    click.echo(f"Assigned {plug_type} plug to {sbc_name}: {address}{idx_str}")


@plug_group.command("remove")
@click.argument("sbc_name")
@click.pass_context
def plug_remove_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Remove power plug assignment from an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    if manager.remove_power_plug(sbc.id):
        click.echo(f"Removed power plug from {sbc_name}")
    else:
        click.echo(f"No power plug assigned to {sbc_name}")


# --- Power Control Commands ---


@main.group("power")
def power_group() -> None:
    """Control power to SBCs."""
    pass


def _get_power_controller(manager: ResourceManager, sbc_name: str) -> tuple:
    """Get power controller for an SBC. Returns (controller, sbc) or exits on error."""
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    if not sbc.power_plug:
        click.echo(f"Error: No power plug assigned to '{sbc_name}'", err=True)
        click.echo("Use 'labctl plug assign' to assign a power plug first.")
        sys.exit(1)

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        return controller, sbc
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _emit_power_event(
    manager: ResourceManager,
    sbc,
    action: str,
    ok: bool,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """Record a power-operation activity event."""
    from labctl.core import audit

    details: dict = {}
    if error:
        details["error"] = error
    if extra:
        details.update(extra)
    audit.emit(
        manager.db,
        action=action,
        entity_type="sbc",
        entity_id=sbc.id,
        entity_name=sbc.name,
        result="ok" if ok else "error",
        details=details or None,
    )


@power_group.command("on")
@click.argument("sbc_name")
@click.pass_context
def power_on_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Turn power on for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Powering on {sbc_name}...")
    try:
        ok = controller.power_on()
        _emit_power_event(manager, sbc, "power_on", ok)
        if ok:
            click.echo(f"Power ON: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power on {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
        _emit_power_event(manager, sbc, "power_on", False, error=str(e))
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@power_group.command("off")
@click.argument("sbc_name")
@click.pass_context
def power_off_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Turn power off for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Powering off {sbc_name}...")
    try:
        ok = controller.power_off()
        _emit_power_event(manager, sbc, "power_off", ok)
        if ok:
            click.echo(f"Power OFF: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power off {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
        _emit_power_event(manager, sbc, "power_off", False, error=str(e))
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@power_group.command("cycle")
@click.argument("sbc_name")
@click.option(
    "--delay",
    "-d",
    type=float,
    default=2.0,
    help="Delay between off and on (default: 2s)",
)
@click.pass_context
def power_cycle_cmd(ctx: click.Context, sbc_name: str, delay: float) -> None:
    """Power cycle an SBC (off, wait, on)."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Power cycling {sbc_name} (delay: {delay}s)...")
    try:
        ok = controller.power_cycle(delay)
        _emit_power_event(
            manager, sbc, "power_cycle", ok, extra={"delay_seconds": delay}
        )
        if ok:
            click.echo(f"Power cycled: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power cycle {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
        _emit_power_event(
            manager,
            sbc,
            "power_cycle",
            False,
            error=str(e),
            extra={"delay_seconds": delay},
        )
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@power_group.command("status")
@click.argument("sbc_name")
@click.pass_context
def power_status_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Show power status for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    try:
        state = controller.get_state()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    plug = sbc.power_plug

    click.echo(f"SBC:    {sbc_name}")
    click.echo(f"Plug:   {plug.plug_type.value} @ {plug.address}")
    if plug.plug_index > 1:
        click.echo(f"Index:  {plug.plug_index}")
    click.echo(f"State:  {state.value.upper()}")


@main.command("power-all")
@click.argument("action", type=click.Choice(["on", "off"]))
@click.option("--project", "-p", help="Filter by project name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def power_all_cmd(
    ctx: click.Context, action: str, project: str | None, yes: bool
) -> None:
    """Turn power on or off for all SBCs."""
    manager = _get_manager(ctx)

    sbcs = manager.list_sbcs(project=project)
    sbcs_with_plugs = [s for s in sbcs if s.power_plug is not None]

    if not sbcs_with_plugs:
        filter_msg = f" in project '{project}'" if project else ""
        click.echo(f"No SBCs with power plugs assigned{filter_msg}.")
        return

    # Show what will be affected
    click.echo(f"SBCs to power {action.upper()}:")
    for sbc in sbcs_with_plugs:
        plug_info = f"{sbc.power_plug.plug_type.value} @ {sbc.power_plug.address}"
        click.echo(f"  - {sbc.name} ({plug_info})")

    if not yes:
        click.confirm(
            f"\nPower {action.upper()} all {len(sbcs_with_plugs)} SBC(s)?", abort=True
        )

    # Execute power commands
    success_count = 0
    for sbc in sbcs_with_plugs:
        try:
            controller = PowerController.from_plug(sbc.power_plug)
            if action == "on":
                result = controller.power_on()
            else:
                result = controller.power_off()

            if result:
                click.echo(f"  {sbc.name}: {action.upper()} OK")
                success_count += 1
            else:
                click.echo(f"  {sbc.name}: FAILED", err=True)
        except Exception as e:
            click.echo(f"  {sbc.name}: ERROR - {e}", err=True)

    click.echo(
        f"\n{success_count}/{len(sbcs_with_plugs)} SBCs powered {action.upper()}"
    )


# --- Console and SSH Commands ---


@main.command("log")
@click.argument("sbc_name")
@click.option(
    "--type",
    "-t",
    "port_type",
    type=click.Choice([t.value for t in PortType]),
    default="console",
    help="Port type (default: console)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output log file (default: <sbc>-<timestamp>.log)",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help="Continuous output (like tail -f)",
)
@click.option(
    "--lines",
    "-n",
    type=int,
    help="Capture N lines then exit",
)
@click.option(
    "--timestamp/--no-timestamp",
    default=True,
    help="Add timestamps to output (default: on)",
)
@click.pass_context
def log_cmd(
    ctx: click.Context,
    sbc_name: str,
    port_type: str,
    output: Path | None,
    follow: bool,
    lines: int | None,
    timestamp: bool,
) -> None:
    """Capture serial output to a log file.

    Connects to an SBC's serial port and logs all output to a file.
    By default, creates a timestamped log file in the current directory.

    \b
    Examples:
      labctl log pi4                     # Log to pi4-<timestamp>.log
      labctl log pi4 -o serial.log       # Log to specific file
      labctl log pi4 -f                  # Follow mode (continuous)
      labctl log pi4 -n 100              # Capture 100 lines then exit
    """
    import select
    import socket
    from datetime import datetime

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    # Find the requested port type
    target_type = PortType(port_type)
    port = None
    for p in sbc.serial_ports:
        if p.port_type == target_type:
            port = p
            break

    if not port:
        click.echo(f"Error: No {port_type} port assigned to '{sbc_name}'", err=True)
        sys.exit(1)

    if not port.tcp_port:
        click.echo("Error: Port has no TCP port configured for logging", err=True)
        sys.exit(1)

    # Determine output file
    if output is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path(f"{sbc_name}-{ts}.log")

    click.echo(f"Logging {sbc_name} to {output}")
    if lines:
        click.echo(f"Will capture {lines} lines then exit")
    else:
        click.echo("Press Ctrl+C to stop")

    line_count = 0
    reconnect_delay = 2

    try:
        with open(output, "a") as f:
            while True:
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect(("localhost", port.tcp_port))
                    sock.setblocking(False)

                    if follow:
                        click.echo(f"Connected to localhost:{port.tcp_port}")

                    buffer = b""

                    while True:
                        readable, _, _ = select.select([sock], [], [], 1.0)
                        if readable:
                            try:
                                data = sock.recv(4096)
                                if not data:
                                    # Connection closed — break to reconnect
                                    break

                                buffer += data

                                while b"\n" in buffer:
                                    line, buffer = buffer.split(b"\n", 1)
                                    try:
                                        line_str = line.decode(
                                            "utf-8", errors="replace"
                                        ).rstrip()
                                    except Exception:
                                        line_str = line.decode("latin-1").rstrip()

                                    if timestamp:
                                        ts_str = datetime.now().strftime(
                                            "%Y-%m-%d %H:%M:%S.%f"
                                        )[:-3]
                                        log_line = f"[{ts_str}] {line_str}\n"
                                    else:
                                        log_line = f"{line_str}\n"

                                    f.write(log_line)
                                    f.flush()

                                    if follow:
                                        click.echo(log_line, nl=False)

                                    line_count += 1

                                    if lines and line_count >= lines:
                                        click.echo(f"\nCaptured {line_count} lines")
                                        return

                            except BlockingIOError:
                                pass

                except (ConnectionRefusedError, OSError):
                    pass
                finally:
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass

                # Reconnect after disconnect or connection failure
                if lines and line_count >= lines:
                    return

                if follow:
                    click.echo(f"Disconnected. Reconnecting in {reconnect_delay}s...")

                import time

                time.sleep(reconnect_delay)

    except KeyboardInterrupt:
        click.echo(f"\nLogged {line_count} lines to {output}")


@main.command("console")
@click.argument("sbc_name")
@click.option(
    "--type",
    "-t",
    "port_type",
    type=click.Choice([t.value for t in PortType]),
    default="console",
    help="Port type (default: console)",
)
@click.pass_context
def console_cmd(ctx: click.Context, sbc_name: str, port_type: str) -> None:
    """Connect to an SBC's serial console.

    Looks up the SBC by name and connects to its configured serial port.
    """
    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    # Find the requested port type
    target_type = PortType(port_type)
    port = None
    for p in sbc.serial_ports:
        if p.port_type == target_type:
            port = p
            break

    if not port:
        click.echo(f"Error: No {port_type} port assigned to '{sbc_name}'", err=True)
        click.echo("Use 'labctl port assign' to assign a serial port first.")
        sys.exit(1)

    # Connect via TCP if available
    if port.tcp_port and config.ser2net.enabled:
        _connect_tcp("localhost", port.tcp_port)
    else:
        _connect_direct(Path(port.device_path), port.baud_rate)


@main.command("ssh")
@click.argument("sbc_name")
@click.option("--user", "-u", help="SSH username (overrides SBC default)")
@click.pass_context
def ssh_cmd(ctx: click.Context, sbc_name: str, user: str | None) -> None:
    """SSH to an SBC.

    Looks up the SBC by name and connects via SSH using its configured IP address.
    """
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    ip = sbc.primary_ip
    if not ip:
        click.echo(f"Error: No IP address configured for '{sbc_name}'", err=True)
        click.echo("Use 'labctl network set' to configure an IP address first.")
        sys.exit(1)

    ssh_user = user or sbc.ssh_user
    click.echo(f"Connecting to {sbc_name} ({ssh_user}@{ip})...")

    try:
        subprocess.run(["ssh", f"{ssh_user}@{ip}"], check=False)
    except FileNotFoundError:
        click.echo("Error: 'ssh' command not found", err=True)
        sys.exit(1)


# --- Status Commands ---


@main.command("status")
@click.option("--project", "-p", help="Filter by project name")
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Continuously update status display",
)
@click.option(
    "--interval",
    "-i",
    type=int,
    default=5,
    help="Watch interval in seconds (default: 5)",
)
@click.pass_context
def status_cmd(
    ctx: click.Context,
    project: str | None,
    watch: bool,
    interval: int,
) -> None:
    """Show status overview of all SBCs.

    \b
    Examples:
      labctl status              # Show status once
      labctl status -w           # Watch mode (updates every 5s)
      labctl status -w -i 10     # Watch mode with 10s interval
    """
    def display_status():
        manager = _get_manager(ctx)
        sbcs = manager.list_sbcs(project=project)

        if not sbcs:
            filter_msg = f" in project '{project}'" if project else ""
            click.echo(f"No SBCs configured{filter_msg}.")
            return False

        # Status colors (ANSI)
        colors = {
            Status.ONLINE: "\033[32m",  # Green
            Status.OFFLINE: "\033[31m",  # Red
            Status.BOOTING: "\033[33m",  # Yellow
            Status.ERROR: "\033[31m",  # Red
            Status.UNKNOWN: "\033[90m",  # Gray
        }
        reset = "\033[0m"

        # Index active claims by sbc_name for O(1) per-row lookup.
        claims_by_sbc = {c.sbc_name: c for c in manager.list_active_claims()}
        power_by_sbc = _collect_status_power_states(sbcs, reset)

        click.echo(
            f"{'NAME':<15} {'PROJECT':<12} {'STATUS':<12} {'IP':<15} "
            f"{'POWER':<6} {'CLAIM':<30}"
        )
        click.echo("-" * 90)

        for sbc in sbcs:
            color = colors.get(sbc.status, "")
            status_str = f"{color}{sbc.status.value:<12}{reset}"
            ip = sbc.primary_ip or "-"
            project_name = sbc.project or "-"
            power = power_by_sbc.get(sbc.name, "-")

            claim = claims_by_sbc.get(sbc.name)
            if claim is None:
                claim_str = "-"
            else:
                remaining = (
                    int(claim.time_remaining.total_seconds())
                    if claim.time_remaining is not None
                    else None
                )
                marker = " ⚠" if claim.pending_requests else ""
                claim_str = (
                    f"{claim.agent_name} ({_format_remaining(remaining)}){marker}"
                )

            click.echo(
                f"{sbc.name:<15} {project_name:<12} {status_str} {ip:<15} "
                f"{power:<6} {claim_str:<30}"
            )

        return True

    if watch:
        from datetime import datetime

        click.echo("Watching status (Ctrl+C to stop)...\n")
        try:
            while True:
                # Clear screen (ANSI escape)
                click.echo("\033[2J\033[H", nl=False)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                click.echo(f"Lab Status (updated: {ts})\n")
                display_status()
                click.echo(f"\nRefreshing every {interval}s... (Ctrl+C to stop)")
                time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nStopped watching")
    else:
        display_status()


# --- Claim Commands ---


def _cli_session_id() -> str:
    """CLI sessions are scoped to user@host so release-by-claimant works across
    invocations from the same user on the same machine."""
    import getpass
    import socket

    return f"cli-{getpass.getuser()}@{socket.gethostname()}"


def _default_agent_name() -> str:
    import getpass

    return getpass.getuser()


def _parse_duration(raw: str) -> int:
    """Parse a duration string like '30m', '2h', or bare minutes into seconds."""
    s = raw.strip().lower()
    if not s:
        raise click.BadParameter("duration must not be empty")
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("s"):
        return int(float(s[:-1]))
    # Bare integer = minutes (matches the spec's 30m/1h/4h phrasing).
    return int(float(s) * 60)


def _validate_duration(ctx: click.Context, seconds: int) -> int:
    """Apply ClaimsConfig min/max bounds; raise UsageError on violation."""
    config: Config = ctx.obj["config"]
    min_s = config.claims.min_duration_minutes * 60
    max_s = config.claims.max_duration_minutes * 60
    if seconds < min_s or seconds > max_s:
        raise click.UsageError(
            f"Duration {seconds}s is out of bounds "
            f"[{min_s}s, {max_s}s] (see claims.min/max_duration_minutes)"
        )
    return seconds


def _format_remaining(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "expired"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _print_claim_row(claim) -> None:
    """Render one claim as a multi-line block on status-like output."""
    remaining_s = (
        int(claim.time_remaining.total_seconds())
        if claim.time_remaining is not None
        else None
    )
    expires_display = claim.expires_at.strftime("%H:%M:%S") if claim.expires_at else "?"
    click.echo(
        f"  {claim.sbc_name:<15} held by {claim.agent_name} "
        f"({claim.session_kind}, expires {expires_display}, "
        f"remaining {_format_remaining(remaining_s)})"
    )
    if claim.reason:
        click.echo(f"      reason: {claim.reason}")
    if claim.renewal_count:
        click.echo(f"      renewed {claim.renewal_count}x")
    if claim.pending_requests:
        for req in claim.pending_requests:
            click.echo(f"      ⚠ release request from {req.requested_by}: {req.reason}")


@main.command("claim")
@click.argument("sbc_name")
@click.option(
    "--duration",
    "-d",
    default=None,
    help="Duration like '30m', '2h', or minutes as integer (default from config)",
)
@click.option("--reason", "-r", required=True, help="Human-readable reason for audit")
@click.option(
    "--name", "-n", "agent_name", default=None, help="Agent identifier (default: $USER)"
)
@click.pass_context
def claim_cmd(
    ctx: click.Context,
    sbc_name: str,
    duration: str | None,
    reason: str,
    agent_name: str | None,
) -> None:
    """Reserve an SBC for exclusive access."""
    from labctl.core.models import ClaimConflict, UnknownSBCError

    config: Config = ctx.obj["config"]
    manager = _get_manager(ctx)

    if duration is None:
        duration_seconds = config.claims.default_duration_minutes * 60
    else:
        duration_seconds = _parse_duration(duration)
    duration_seconds = _validate_duration(ctx, duration_seconds)

    try:
        claim = manager.claim_sbc(
            sbc_name=sbc_name,
            agent_name=agent_name or _default_agent_name(),
            session_id=_cli_session_id(),
            session_kind="cli",
            duration_seconds=duration_seconds,
            reason=reason,
            grace_seconds=config.claims.grace_period_seconds,
        )
    except UnknownSBCError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except ClaimConflict as exc:
        holder = exc.claim.agent_name or "unknown"
        remaining = (
            int(exc.claim.time_remaining.total_seconds())
            if exc.claim.time_remaining is not None
            else 0
        )
        click.echo(
            f"Error: '{sbc_name}' is already claimed by '{holder}' "
            f"({_format_remaining(remaining)} remaining). "
            f"Use 'labctl request-release' or 'labctl force-release'.",
            err=True,
        )
        sys.exit(1)

    expires = (
        claim.expires_at.strftime("%Y-%m-%d %H:%M:%S") if claim.expires_at else "?"
    )
    click.echo(
        f"Claimed '{sbc_name}' as '{claim.agent_name}' until {expires} "
        f"({_format_remaining(int(claim.time_remaining.total_seconds()))} from now)"
    )


@main.command("release")
@click.argument("sbc_name")
@click.pass_context
def release_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Release a claim on an SBC."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    manager = _get_manager(ctx)
    try:
        manager.release_claim(sbc_name, _cli_session_id())
    except (ClaimNotFoundError, NotClaimantError, UnknownSBCError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Released claim on '{sbc_name}'")


@main.command("renew")
@click.argument("sbc_name")
@click.option(
    "--duration",
    "-d",
    default=None,
    help="New duration (default: keep previous duration but reset from now)",
)
@click.pass_context
def renew_cmd(ctx: click.Context, sbc_name: str, duration: str | None) -> None:
    """Extend an active claim's deadline."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    manager = _get_manager(ctx)
    duration_seconds = None
    if duration is not None:
        duration_seconds = _validate_duration(ctx, _parse_duration(duration))

    try:
        claim = manager.renew_claim(
            sbc_name, _cli_session_id(), duration_seconds=duration_seconds
        )
    except (ClaimNotFoundError, NotClaimantError, UnknownSBCError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    expires = (
        claim.expires_at.strftime("%Y-%m-%d %H:%M:%S") if claim.expires_at else "?"
    )
    click.echo(f"Renewed claim on '{sbc_name}' until {expires}")


@main.command("force-release")
@click.argument("sbc_name")
@click.option("--reason", "-r", required=True, help="Why the override is needed")
@click.pass_context
def force_release_cmd(ctx: click.Context, sbc_name: str, reason: str) -> None:
    """Operator override — forcibly release an active claim."""
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    manager = _get_manager(ctx)
    try:
        released = manager.force_release_claim(
            sbc_name, reason, released_by=_default_agent_name()
        )
    except (ClaimNotFoundError, UnknownSBCError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(
        f"Force-released claim on '{sbc_name}' "
        f"(was held by '{released.agent_name}'): {reason}"
    )


@main.command("request-release")
@click.argument("sbc_name")
@click.option("--reason", "-r", required=True, help="Why you need the SBC")
@click.pass_context
def request_release_cmd(ctx: click.Context, sbc_name: str, reason: str) -> None:
    """Politely ask the current claimant to release an SBC."""
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    manager = _get_manager(ctx)
    try:
        manager.record_release_request(
            sbc_name, requested_by=_default_agent_name(), reason=reason
        )
    except (ClaimNotFoundError, UnknownSBCError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(
        f"Release request recorded on '{sbc_name}'. "
        f"Claimant will see it on their next operation."
    )


@main.group("claims")
def claims_group() -> None:
    """Inspect active and past claims."""


@claims_group.command("list")
@click.pass_context
def claims_list_cmd(ctx: click.Context) -> None:
    """List all active claims across the lab."""
    manager = _get_manager(ctx)
    claims = manager.list_active_claims()
    if not claims:
        click.echo("No active claims.")
        return
    click.echo(f"{len(claims)} active claim(s):")
    for claim in claims:
        _print_claim_row(claim)


@claims_group.command("show")
@click.argument("sbc_name")
@click.pass_context
def claims_show_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Show the active claim on an SBC, including pending release requests."""
    from labctl.core.models import UnknownSBCError

    manager = _get_manager(ctx)
    try:
        claim = manager.get_active_claim(sbc_name)
    except UnknownSBCError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if claim is None:
        click.echo(f"No active claim on '{sbc_name}'.")
        return
    _print_claim_row(claim)


@claims_group.command("history")
@click.argument("sbc_name")
@click.option("--last", "-n", type=int, default=10, help="How many entries to show")
@click.pass_context
def claims_history_cmd(ctx: click.Context, sbc_name: str, last: int) -> None:
    """Show past claims for an SBC."""
    from labctl.core.models import UnknownSBCError

    manager = _get_manager(ctx)
    try:
        history = manager.list_claim_history(sbc_name, limit=last)
    except UnknownSBCError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if not history:
        click.echo(f"No claim history for '{sbc_name}'.")
        return
    click.echo(f"Last {len(history)} claim(s) on '{sbc_name}':")
    for claim in history:
        reason_str = claim.release_reason.value if claim.release_reason else "unknown"
        acquired = (
            claim.acquired_at.strftime("%Y-%m-%d %H:%M:%S")
            if claim.acquired_at
            else "?"
        )
        released = (
            claim.released_at.strftime("%Y-%m-%d %H:%M:%S")
            if claim.released_at
            else "?"
        )
        click.echo(
            f"  {claim.agent_name} ({claim.session_kind}): "
            f"{acquired} → {released}  [{reason_str}]"
        )
        if claim.reason:
            click.echo(f"      reason: {claim.reason}")


@claims_group.command("expire")
@click.pass_context
def claims_expire_cmd(ctx: click.Context) -> None:
    """Run one sweep: release expired claims and dead-session claims.

    Suitable for cron / systemd timer (e.g. every 60 seconds).
    """
    config: Config = ctx.obj["config"]
    manager = _get_manager(ctx)
    grace = config.claims.grace_period_seconds
    expired = manager.expire_stale_claims(grace_seconds=grace)
    dead = manager.release_dead_sessions(grace_seconds=grace)
    pruned = manager.prune_released_claims(
        older_than_days=config.claims.auto_prune_released_after_days
    )
    parts = []
    if expired or dead:
        parts.append(f"{expired} expired + {dead} dead-session released")
    if pruned:
        parts.append(f"{pruned} old claim(s) pruned")
    if parts:
        click.echo("; ".join(parts))
    else:
        click.echo("No stale claims found")


@claims_group.command("stats")
@click.pass_context
def claims_stats_cmd(ctx: click.Context) -> None:
    """Show aggregate claim statistics."""
    manager = _get_manager(ctx)
    m = manager.get_claim_metrics()
    click.echo(f"Total claims:     {m['total']}")
    click.echo(f"  Active:         {m['active']}")
    click.echo(f"  Released:       {m['released']}")
    click.echo(f"  Expired:        {m['expired']}")
    click.echo(f"  Force-released: {m['force_released']}")
    click.echo(f"  Session-lost:   {m['session_lost']}")
    if m["avg_duration_seconds"] is not None:
        avg = m["avg_duration_seconds"]
        if avg >= 3600:
            click.echo(f"  Avg duration:   {avg // 3600}h {(avg % 3600) // 60}m")
        elif avg >= 60:
            click.echo(f"  Avg duration:   {avg // 60}m {avg % 60}s")
        else:
            click.echo(f"  Avg duration:   {avg}s")


# --- Export/Import Commands ---


@main.command("export")
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
    help="Output format (default: yaml)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: stdout)",
)
@click.pass_context
def export_cmd(ctx: click.Context, fmt: str, output: Path | None) -> None:
    """Export all SBC configurations."""
    import json

    import yaml

    manager = _get_manager(ctx)
    sbcs = manager.list_sbcs()

    if not sbcs:
        click.echo("No SBCs to export.")
        return

    # Build export data — serial devices first
    serial_devices = manager.list_serial_devices()
    data = {}

    if serial_devices:
        data["serial_devices"] = []
        for sd in serial_devices:
            sd_data = {"name": sd.name, "usb_path": sd.usb_path}
            if sd.vendor:
                sd_data["vendor"] = sd.vendor
            if sd.model:
                sd_data["model"] = sd.model
            if sd.serial_number:
                sd_data["serial_number"] = sd.serial_number
            data["serial_devices"].append(sd_data)

    data["sbcs"] = []
    for sbc in sbcs:
        sbc_data = {
            "name": sbc.name,
            "project": sbc.project,
            "description": sbc.description,
            "ssh_user": sbc.ssh_user,
            "status": sbc.status.value,
        }

        # Add serial ports
        if sbc.serial_ports:
            sbc_data["serial_ports"] = []
            for port in sbc.serial_ports:
                port_data = {
                    "type": port.port_type.value,
                    "device": port.device_path,
                    "tcp_port": port.tcp_port,
                    "baud_rate": port.baud_rate,
                }
                if port.alias:
                    port_data["alias"] = port.alias
                if port.serial_device:
                    port_data["serial_device"] = port.serial_device.name
                sbc_data["serial_ports"].append(port_data)

        # Add network addresses
        if sbc.network_addresses:
            sbc_data["network_addresses"] = []
            for addr in sbc.network_addresses:
                addr_data = {
                    "type": addr.address_type.value,
                    "ip": addr.ip_address,
                }
                if addr.mac_address:
                    addr_data["mac"] = addr.mac_address
                if addr.hostname:
                    addr_data["hostname"] = addr.hostname
                sbc_data["network_addresses"].append(addr_data)

        # Add power plug
        if sbc.power_plug:
            sbc_data["power_plug"] = {
                "type": sbc.power_plug.plug_type.value,
                "address": sbc.power_plug.address,
                "index": sbc.power_plug.plug_index,
            }

        # Add SDWire
        if sbc.sdwire:
            sbc_data["sdwire"] = {
                "name": sbc.sdwire.name,
                "serial_number": sbc.sdwire.serial_number,
                "device_type": sbc.sdwire.device_type,
            }

        data["sbcs"].append(sbc_data)

    # Format output
    if fmt == "yaml":
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)
    else:
        content = json.dumps(data, indent=2)

    if output:
        output.write_text(content)
        click.echo(f"Exported {len(sbcs)} SBC(s) to {output}")
    else:
        click.echo(content)


@main.command("import")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--update", "-u", is_flag=True, help="Update existing SBCs instead of skipping"
)
@click.pass_context
def import_cmd(ctx: click.Context, file: Path, update: bool) -> None:
    """Import SBC configurations from file."""
    import json

    import yaml

    manager = _get_manager(ctx)

    # Load file
    content = file.read_text()
    if file.suffix in [".yaml", ".yml"]:
        data = yaml.safe_load(content)
    elif file.suffix == ".json":
        data = json.loads(content)
    else:
        # Try YAML first, then JSON
        try:
            data = yaml.safe_load(content)
        except Exception:
            data = json.loads(content)

    # Import serial devices first (must exist before port references)
    for sd_data in data.get("serial_devices", []):
        sd_name = sd_data.get("name")
        if not sd_name:
            continue
        existing_sd = manager.get_serial_device_by_name(sd_name)
        if existing_sd:
            if update:
                click.echo(f"  Serial device already exists: {sd_name}")
            continue
        try:
            manager.create_serial_device(
                name=sd_name,
                usb_path=sd_data.get("usb_path", ""),
                vendor=sd_data.get("vendor"),
                model=sd_data.get("model"),
                serial_number=sd_data.get("serial_number"),
            )
            click.echo(f"  Created serial device: {sd_name}")
        except Exception as e:
            click.echo(
                f"  Warning: Failed to create serial device {sd_name}: {e}", err=True
            )

    sbcs_data = data.get("sbcs", [])
    if not sbcs_data:
        click.echo("No SBCs found in import file.")
        return

    created = 0
    updated = 0
    skipped = 0

    for sbc_data in sbcs_data:
        name = sbc_data.get("name")
        if not name:
            click.echo("Warning: Skipping SBC without name", err=True)
            skipped += 1
            continue

        existing = manager.get_sbc_by_name(name)

        if existing and not update:
            click.echo(f"  Skipping existing SBC: {name}")
            skipped += 1
            continue

        if existing:
            # Update existing
            manager.update_sbc(
                existing.id,
                project=sbc_data.get("project"),
                description=sbc_data.get("description"),
                ssh_user=sbc_data.get("ssh_user"),
            )
            sbc = manager.get_sbc(existing.id)
            click.echo(f"  Updated: {name}")
            updated += 1
        else:
            # Create new
            sbc = manager.create_sbc(
                name=name,
                project=sbc_data.get("project"),
                description=sbc_data.get("description"),
                ssh_user=sbc_data.get("ssh_user", "root"),
            )
            click.echo(f"  Created: {name}")
            created += 1

        # Import serial ports
        for port_data in sbc_data.get("serial_ports", []):
            sd_id = None
            sd_name = port_data.get("serial_device")
            if sd_name:
                sd = manager.get_serial_device_by_name(sd_name)
                if sd:
                    sd_id = sd.id
            manager.assign_serial_port(
                sbc_id=sbc.id,
                port_type=PortType(port_data["type"]),
                device_path=port_data["device"],
                tcp_port=port_data.get("tcp_port"),
                baud_rate=port_data.get("baud_rate", 115200),
                alias=port_data.get("alias"),
                serial_device_id=sd_id,
            )

        # Import network addresses
        for addr_data in sbc_data.get("network_addresses", []):
            manager.set_network_address(
                sbc_id=sbc.id,
                address_type=AddressType(addr_data["type"]),
                ip_address=addr_data["ip"],
                mac_address=addr_data.get("mac"),
                hostname=addr_data.get("hostname"),
            )

        # Import power plug
        if "power_plug" in sbc_data:
            plug_data = sbc_data["power_plug"]
            manager.assign_power_plug(
                sbc_id=sbc.id,
                plug_type=PlugType(plug_data["type"]),
                address=plug_data["address"],
                plug_index=plug_data.get("index", 1),
            )

        # Import SDWire assignment
        if "sdwire" in sbc_data:
            sw_data = sbc_data["sdwire"]
            sw_name = sw_data.get("name")
            if sw_name:
                # Ensure the SDWire device exists
                sw_device = manager.get_sdwire_device_by_name(sw_name)
                if not sw_device:
                    try:
                        sw_device = manager.create_sdwire_device(
                            name=sw_name,
                            serial_number=sw_data.get("serial_number", ""),
                            device_type=sw_data.get("device_type", "sdwirec"),
                        )
                    except Exception:
                        pass
                if sw_device:
                    try:
                        manager.assign_sdwire(sbc.id, sw_device.id)
                    except Exception:
                        pass

    click.echo(
        f"\nImport complete: {created} created, {updated} updated, {skipped} skipped"
    )


# --- Proxy Commands ---

# Global proxy manager instance (for CLI session)
_proxy_manager = None


def _get_proxy_manager(ctx: click.Context):
    """Get or create proxy manager from context."""
    global _proxy_manager
    if _proxy_manager is None:
        config: Config = ctx.obj["config"]
        from labctl.serial.proxy import ProxyManager

        _proxy_manager = ProxyManager(log_dir=config.proxy.log_dir)
    return _proxy_manager


@main.group("proxy")
def proxy_group() -> None:
    """Manage serial proxy for multi-client access."""
    pass


@proxy_group.command("start")
@click.argument("sbc_name")
@click.option(
    "--port", "-p", type=int, help="Proxy port (auto-assigned if not specified)"
)
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (default)")
@click.pass_context
def proxy_start_cmd(
    ctx: click.Context, sbc_name: str, port: int | None, foreground: bool
) -> None:
    """Start a serial proxy for an SBC.

    Allows multiple clients to connect to the same serial console.
    First client to send data gets write access, others are read-only.
    """
    import asyncio

    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    # Find console port
    console_port = sbc.console_port
    if not console_port:
        click.echo(f"Error: No console port assigned to '{sbc_name}'", err=True)
        click.echo("Use 'labctl port assign' to assign a serial port first.")
        sys.exit(1)

    if not console_port.tcp_port:
        click.echo("Error: Console port has no TCP port configured", err=True)
        sys.exit(1)

    # Determine proxy port
    if port is None:
        port = config.proxy.port_base
        # Find next available port (simple increment)
        proxy_mgr = _get_proxy_manager(ctx)
        port = proxy_mgr.get_next_port(config.proxy.port_base, config.proxy.port_range)

    click.echo(f"Starting proxy for {sbc_name}...")
    click.echo(f"  ser2net port: {console_port.tcp_port}")
    click.echo(f"  proxy port:   {port}")
    click.echo(f"  write policy: {config.proxy.write_policy}")
    if config.proxy.log_dir:
        click.echo(f"  session logs: {config.proxy.log_dir}")
    click.echo()
    click.echo("Press Ctrl+C to stop the proxy")
    click.echo("-" * 40)

    async def run_proxy():
        from labctl.serial.proxy import SerialProxy

        proxy = SerialProxy(
            name=sbc_name,
            ser2net_host="localhost",
            ser2net_port=console_port.tcp_port,
            proxy_port=port,
            log_dir=config.proxy.log_dir if config.proxy.log_dir else None,
            write_policy=config.proxy.write_policy,
            max_clients=config.proxy.max_clients,
        )

        try:
            await proxy.start()
            click.echo(f"Proxy running on port {port}")
            click.echo("Connect with: nc localhost {port}")

            # Keep running until cancelled
            while proxy.is_running:
                await asyncio.sleep(1)
        except ConnectionError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except KeyboardInterrupt:
            pass
        finally:
            await proxy.stop()
            click.echo("Proxy stopped")

    try:
        asyncio.run(run_proxy())
    except KeyboardInterrupt:
        pass


@proxy_group.command("list")
@click.pass_context
def proxy_list_cmd(ctx: click.Context) -> None:
    """List running proxies.

    Note: This only shows proxies started in daemon mode (not implemented yet).
    For foreground proxies, they run until Ctrl+C.
    """
    # In the current implementation, proxies run in foreground
    # A proper daemon mode would require a separate process or service
    click.echo("Note: Proxy daemon mode not yet implemented.")
    click.echo("Use 'labctl proxy start <sbc>' to run a proxy in foreground.")
    click.echo("Each proxy runs until Ctrl+C.")


@main.command("sessions")
@click.argument("sbc_name", required=False)
@click.pass_context
def sessions_cmd(ctx: click.Context, sbc_name: str | None) -> None:
    """List connected proxy sessions.

    Shows clients connected to each SBC's proxy.
    If SBC_NAME is provided, shows only that SBC's sessions.

    Note: Currently only works for proxies started with daemon mode.
    """
    # This would query a running proxy daemon for session info
    # For now, just show a placeholder message
    click.echo(
        "Note: Session listing requires proxy daemon mode (not yet implemented)."
    )
    click.echo()
    click.echo("When running a proxy in foreground ('labctl proxy start <sbc>'),")
    click.echo("client connections are logged to the console and session log files.")


# --- MCP Server ---


@main.command("mcp")
@click.option(
    "--http",
    "http_port",
    type=int,
    default=None,
    help="Run as HTTP server on this port (default: stdio transport)",
)
@click.pass_context
def mcp_cmd(ctx: click.Context, http_port: int | None) -> None:
    """Start the MCP (Model Context Protocol) server.

    By default uses stdio transport for local AI tool integration
    (Claude Desktop, Claude Code, etc.). Use --http for remote access.
    """
    from labctl.mcp_server import run_server

    if http_port:
        click.echo(f"Starting MCP server (HTTP on port {http_port})...")
        run_server(transport="http", http_port=http_port)
    else:
        # stdio mode — no output to stdout (it's the JSON-RPC channel)
        run_server(transport="stdio")


# --- Web Server ---


@main.command("web")
@click.option(
    "--host", "-h", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
)
@click.option(
    "--port", "-p", type=int, default=5000, help="Port to bind to (default: 5000)"
)
@click.option("--debug", is_flag=True, help="Enable debug mode")
@click.option(
    "--cert",
    type=click.Path(exists=True),
    default=None,
    help="Path to SSL certificate file (PEM format)",
)
@click.option(
    "--key",
    type=click.Path(exists=True),
    default=None,
    help="Path to SSL private key file (PEM format)",
)
@click.pass_context
def web_cmd(
    ctx: click.Context,
    host: str,
    port: int,
    debug: bool,
    cert: str | None,
    key: str | None,
) -> None:
    """Start the web dashboard server."""
    from labctl.web import create_app

    config: Config = ctx.obj["config"]
    app = create_app(config)

    # Resolve SSL: CLI flags take precedence, then config file
    ssl_cert = cert or config.web.cert_file or None
    ssl_key = key or config.web.key_file or None

    if bool(ssl_cert) != bool(ssl_key):
        click.echo("Error: Both --cert and --key are required for SSL.", err=True)
        ctx.exit(1)

    ssl_context = (ssl_cert, ssl_key) if ssl_cert and ssl_key else None
    scheme = "https" if ssl_context else "http"

    click.echo("Starting Lab Controller web server...")
    click.echo(f"Dashboard: {scheme}://{host}:{port}/")
    click.echo(f"API:       {scheme}://{host}:{port}/api/")
    click.echo("Press Ctrl+C to stop")

    app.run(host=host, port=port, debug=debug, ssl_context=ssl_context)


# --- Boot Test ---


@main.command("boot-test")
@click.argument("sbc_name")
@click.option(
    "--image",
    "-i",
    type=click.Path(exists=True),
    help="Image file to deploy before testing",
)
@click.option(
    "--dest",
    "-d",
    help="Destination filename on SD card (required with --image)",
)
@click.option(
    "--partition",
    "-p",
    type=int,
    default=1,
    help="Partition number for deploy (default: 1)",
)
@click.option(
    "--expect",
    "-e",
    "expect_pattern",
    required=True,
    help="Regex pattern that indicates successful boot",
)
@click.option(
    "--runs",
    "-r",
    type=int,
    default=10,
    help="Number of boot cycles (default: 10)",
)
@click.option(
    "--timeout",
    "-t",
    type=float,
    default=30.0,
    help="Seconds to wait per boot (default: 30)",
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Save per-run output to this directory",
)
@click.option(
    "--no-deploy",
    is_flag=True,
    help="Skip deploy, test current SD card contents",
)
@click.pass_context
def boot_test_cmd(
    ctx: click.Context,
    sbc_name: str,
    image: str | None,
    dest: str | None,
    partition: int,
    expect_pattern: str,
    runs: int,
    timeout: float,
    output_dir: str | None,
    no_deploy: bool,
) -> None:
    """Automated boot reliability testing.

    Deploys an image (optional), then reboots the SBC multiple times and
    captures serial output to determine success rate.

    \b
    Examples:
      labctl boot-test pi-5-1 -i slmos.bin -d kernel_2712.img -p 1 -e "slmos>" -r 10
      labctl boot-test pi-5-1 --no-deploy -e "slmos>" -r 5 -t 30
      labctl boot-test pi-5-1 -i slmos.bin -d kernel_2712.img -e "slmos>" -o /tmp/results/
    """
    import time as time_mod

    from labctl.serial.boot_test import run_boot_test

    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    # Validate serial console
    if not sbc.console_port or not sbc.console_port.tcp_port:
        click.echo(
            f"Error: SBC '{sbc_name}' has no console port with TCP configured",
            err=True,
        )
        sys.exit(1)

    # Validate power control
    if not sbc.power_plug:
        click.echo(
            f"Error: SBC '{sbc_name}' has no power plug assigned",
            err=True,
        )
        sys.exit(1)

    # Validate deploy args
    if not no_deploy:
        if not image:
            click.echo("Error: --image is required (or use --no-deploy)", err=True)
            sys.exit(1)
        if not dest:
            click.echo("Error: --dest is required with --image", err=True)
            sys.exit(1)

    # Build deploy function
    deploy_fn = None
    if not no_deploy and image and dest:

        def deploy_fn():
            from labctl.sdwire.controller import SDWireController

            if not sbc.sdwire:
                raise RuntimeError(f"No SDWire assigned to '{sbc_name}'")

            ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

            click.echo("Deploying image...")
            ctrl.switch_to_host()
            time_mod.sleep(2)
            ctrl.update_files(partition, [(image, dest)])
            ctrl.switch_to_dut()
            click.echo(f"Deployed {image} -> {dest}")

    # Build power cycle function
    def power_cycle_fn():
        from labctl.power import PowerController

        power_ctrl = PowerController.from_plug(sbc.power_plug)
        power_ctrl.power_cycle()

    # Progress callback
    def progress(run_num, total, run_result):
        status = "PASS" if run_result.passed else "FAIL"
        click.echo(
            f"Run {run_num:2d}/{total}: {status} "
            f"({run_result.elapsed_seconds:.1f}s)"
        )

    click.echo(
        f"Boot test: {sbc_name}, {runs} runs, "
        f"pattern='{expect_pattern}', timeout={timeout}s"
    )
    click.echo("")

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
            progress_fn=progress,
        )

        click.echo("")
        click.echo(result.format_summary())

    except RuntimeError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)


# --- Health Check Commands ---


@main.command("health-check")
@click.option(
    "--type",
    "-t",
    "check_type",
    type=click.Choice(["ping", "serial", "power", "all"]),
    default="all",
    help="Type of health check to run (default: all)",
)
@click.option("--sbc", "-s", "sbc_name", help="Check only this SBC")
@click.option("--update", "-u", is_flag=True, help="Update SBC status in database")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed check results")
@click.pass_context
def health_check_cmd(
    ctx: click.Context,
    check_type: str,
    sbc_name: str | None,
    update: bool,
    verbose: bool,
) -> None:
    """Run health checks on SBCs.

    Performs ping, serial, and power checks to verify SBC health.
    Results are displayed in a table format.

    \b
    Examples:
      labctl health-check              # Check all SBCs
      labctl health-check --sbc pi4    # Check single SBC
      labctl health-check -t ping      # Ping check only
      labctl health-check -u           # Update status in database
    """
    from labctl.health import CheckType, HealthChecker, format_check_table

    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    # Create health checker
    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )

    # Determine which checks to run
    if check_type == "all":
        types = [CheckType.PING, CheckType.SERIAL, CheckType.POWER]
    else:
        types = [CheckType(check_type)]

    # Get SBCs to check
    if sbc_name:
        sbc = manager.get_sbc_by_name(sbc_name)
        if not sbc:
            click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
            sys.exit(1)
        sbcs = [sbc]
    else:
        sbcs = manager.list_sbcs()

    if not sbcs:
        click.echo("No SBCs found.")
        return

    click.echo(f"Running health checks on {len(sbcs)} SBC(s)...\n")

    # Run checks
    results = checker.check_all(sbcs, types)

    # Display results
    click.echo(format_check_table(results, show_details=verbose))

    # Update status if requested
    if update:
        updated = 0
        for sbc_name_key, summary in results.items():
            if summary.recommended_status:
                sbc = manager.get_sbc_by_name(sbc_name_key)
                if sbc and sbc.status != summary.recommended_status:
                    manager.update_sbc(sbc.id, status=summary.recommended_status)
                    # Build details
                    details_parts = []
                    if summary.ping_result:
                        details_parts.append(summary.ping_result.message)
                    if summary.serial_result:
                        details_parts.append(summary.serial_result.message)
                    details = "; ".join(details_parts) if details_parts else None
                    manager.log_status(sbc.id, summary.recommended_status, details)
                    updated += 1
        if updated > 0:
            click.echo(f"\nUpdated status for {updated} SBC(s)")


@main.command("monitor")
@click.option(
    "--interval",
    "-i",
    type=int,
    default=None,
    help="Check interval in seconds (default: from config)",
)
@click.option(
    "--no-update",
    is_flag=True,
    help="Don't update SBC status in database",
)
@click.option(
    "--no-alerts",
    is_flag=True,
    help="Don't trigger alerts on status changes",
)
@click.pass_context
def monitor_cmd(
    ctx: click.Context,
    interval: int | None,
    no_update: bool,
    no_alerts: bool,
) -> None:
    """Start the monitoring daemon.

    Runs periodic health checks on all SBCs and triggers alerts
    when status changes occur.

    Press Ctrl+C to stop.

    \b
    Examples:
      labctl monitor                   # Start with default interval
      labctl monitor -i 30             # Check every 30 seconds
      labctl monitor --no-alerts       # Disable alerting
    """
    from labctl.health import (
        AlertManager,
        ConsoleAlertHandler,
        HealthChecker,
        LogAlertHandler,
        MonitorDaemon,
    )

    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    # Use interval from args or config
    check_interval = interval or config.health.check_interval

    # Create health checker
    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )

    # Create alert manager
    alert_manager = AlertManager()
    if not no_alerts:
        # Add console handler for immediate feedback
        alert_manager.add_handler(ConsoleAlertHandler())
        # Add log file handler
        alert_manager.add_handler(LogAlertHandler(config.health.alert_log_path))

    # Create and start daemon
    daemon = MonitorDaemon(
        manager=manager,
        checker=checker,
        alert_manager=alert_manager,
        interval=check_interval,
        update_status=not no_update,
        alert_on_offline=config.health.alert_on_offline,
        alert_on_power_change=config.health.alert_on_power_change,
    )

    click.echo(f"Starting monitor daemon (interval: {check_interval}s)")
    click.echo("Press Ctrl+C to stop\n")

    try:
        daemon.start()
    except KeyboardInterrupt:
        pass
    finally:
        alert_manager.close()
        click.echo("\nMonitor stopped")


# --- Shell Completion ---


@main.group("services")
def services_group() -> None:
    """Check the systemd services labctl depends on."""
    pass


@services_group.command("status")
@click.option(
    "--unit",
    "extra_units",
    multiple=True,
    help="Additional systemd unit to check (may be repeated)",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Show recent error log lines even for healthy services",
)
def services_status_cmd(extra_units: tuple[str, ...], verbose: bool) -> None:
    """Show status of labctl's services plus ser2net.

    Queries the following units by default:

      labctl-monitor  labctl-mcp  labctl-web  ser2net

    Reports ActiveState/SubState, uptime, systemd restart counter, and
    recent error-level journal lines when a unit is unhealthy or has
    restarted. Exits with code 1 if any unit is not active/running.
    """
    from labctl.services import DEFAULT_UNITS, check_all

    units = DEFAULT_UNITS + tuple(extra_units)
    statuses = check_all(units)

    click.echo(
        f"{'UNIT':<20} {'STATE':<10} {'SUB':<10} {'UPTIME':<10} {'RESTARTS':<10} NOTES"
    )
    click.echo("-" * 80)
    any_unhealthy = False
    for s in statuses:
        if s.error:
            note = s.error
            state_color = "red"
            any_unhealthy = True
        elif s.active_state == "failed":
            note = f"last result: {s.result or '-'}"
            state_color = "red"
            any_unhealthy = True
        elif not s.healthy:
            note = f"sub={s.sub_state}"
            state_color = "yellow"
            any_unhealthy = True
        else:
            note = "-"
            state_color = "green"

        click.echo(
            f"{s.unit:<20} "
            f"{click.style(s.active_state, fg=state_color):<10} "
            f"{s.sub_state:<10} "
            f"{s.uptime_str():<10} "
            f"{s.n_restarts:<10} "
            f"{note}"
        )

        if s.recent_errors and (verbose or not s.healthy or s.n_restarts > 0):
            for line in s.recent_errors:
                click.echo(f"    {line}")

    if any_unhealthy:
        sys.exit(1)


@main.group("activity")
def activity_group() -> None:
    """View the activity stream — every state-changing action."""
    pass


def _parse_since(since: str) -> str:
    """Parse a relative time spec like "5m", "2h", "1d" into an ISO timestamp.

    Returns the ISO8601 string (UTC-naive, matching CURRENT_TIMESTAMP writes).
    Raises click.BadParameter on malformed input.
    """
    from datetime import datetime, timedelta

    since = since.strip().lower()
    if not since:
        raise click.BadParameter("--since cannot be empty")
    unit = since[-1]
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    if unit not in units:
        raise click.BadParameter(
            f"Unknown unit '{unit}'. Use Ns, Nm, Nh, or Nd."
        )
    try:
        n = int(since[:-1])
    except ValueError as e:
        raise click.BadParameter(f"Invalid number: {since[:-1]}") from e
    cutoff = datetime.now() - timedelta(**{units[unit]: n})
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _format_activity_row(row) -> str:
    """Render a single audit_log row as a colored line."""
    ts = row["logged_at"] or ""
    actor = row["actor"] or "internal"
    action = row["action"] or ""
    target = row["entity_name"] or "-"
    result = row["result"] or "ok"
    result_color = {"ok": "green", "error": "red", "forbidden": "yellow"}.get(
        result, "white"
    )
    line = (
        f"{ts}  "
        f"{click.style(actor, fg='cyan'):<32} "
        f"{action:<24} "
        f"{target:<20} "
        f"{click.style(result, fg=result_color)}"
    )
    if row["details"]:
        line += f"  {row['details']}"
    return line


def _activity_query_clauses(
    sbc_filter: str | None,
    actor_filter: str | None,
    source_filter: str | None,
    result_filter: str | None,
    since: str | None,
) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if sbc_filter:
        where.append("entity_name = ?")
        params.append(sbc_filter)
    if actor_filter:
        where.append("actor = ?")
        params.append(actor_filter)
    if source_filter:
        where.append("source = ?")
        params.append(source_filter)
    if result_filter:
        where.append("result = ?")
        params.append(result_filter)
    if since:
        where.append("logged_at >= ?")
        params.append(_parse_since(since))
    return where, params


@activity_group.command("tail")
@click.option("--sbc", "sbc_filter", help="Filter to a single SBC name")
@click.option("--actor", "actor_filter", help="Filter to one actor (exact match)")
@click.option(
    "--source",
    "source_filter",
    type=click.Choice(["cli", "mcp", "api", "web", "daemon", "internal"]),
    help="Filter to one source",
)
@click.option(
    "--result",
    "result_filter",
    type=click.Choice(["ok", "error", "forbidden"]),
    help="Filter to one result",
)
@click.option(
    "--since",
    help="Only show events since this relative time (e.g., 5m, 2h, 1d)",
)
@click.option(
    "-n",
    "--limit",
    type=int,
    default=50,
    help="Maximum number of events to show (default: 50)",
)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Stream new events as they arrive (polls the database every 500ms)",
)
@click.pass_context
def activity_tail_cmd(
    ctx: click.Context,
    sbc_filter: str | None,
    actor_filter: str | None,
    source_filter: str | None,
    result_filter: str | None,
    since: str | None,
    limit: int,
    follow: bool,
) -> None:
    """Show recent activity events.

    Events are returned in chronological order (oldest shown first).
    Use `--follow` to stream new events live; poll the DB directly so
    this works whether or not the web service is running.
    """
    manager = _get_manager(ctx)

    where, params = _activity_query_clauses(
        sbc_filter, actor_filter, source_filter, result_filter, since
    )

    sql = (
        "SELECT id, logged_at, actor, source, action, entity_name, "
        "result, details "
        "FROM audit_log"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params_initial = list(params) + [limit]

    rows = manager.db.execute(sql, tuple(params_initial))
    if not rows and not follow:
        click.echo("(no events)")
        return

    last_id = 0
    for row in reversed(rows):
        click.echo(_format_activity_row(row))
        last_id = max(last_id, row["id"])

    if not follow:
        return

    # Follow mode: poll for new rows every 500ms.
    follow_sql = (
        "SELECT id, logged_at, actor, source, action, entity_name, "
        "result, details FROM audit_log WHERE id > ?"
    )
    follow_where = list(where)
    follow_params_base = list(params)
    if follow_where:
        follow_sql += " AND " + " AND ".join(follow_where)
    follow_sql += " ORDER BY id ASC LIMIT 500"

    import time

    try:
        while True:
            time.sleep(0.5)
            new_rows = manager.db.execute(
                follow_sql, tuple([last_id] + follow_params_base)
            )
            for row in new_rows:
                click.echo(_format_activity_row(row))
                last_id = row["id"]
    except KeyboardInterrupt:
        pass


@activity_group.command("export")
@click.option("--sbc", "sbc_filter", help="Filter to a single SBC name")
@click.option("--actor", "actor_filter", help="Filter to one actor (exact match)")
@click.option(
    "--source",
    "source_filter",
    type=click.Choice(["cli", "mcp", "api", "web", "daemon", "internal"]),
    help="Filter to one source",
)
@click.option(
    "--result",
    "result_filter",
    type=click.Choice(["ok", "error", "forbidden"]),
    help="Filter to one result",
)
@click.option(
    "--since",
    help="Only export events since this relative time (e.g., 5m, 2h, 1d)",
)
@click.option(
    "-n",
    "--limit",
    type=int,
    default=1000,
    help="Maximum number of events to export (default: 1000)",
)
@click.option(
    "--format",
    "export_format",
    type=click.Choice(["ndjson"]),
    default="ndjson",
    show_default=True,
    help="Export format",
)
@click.pass_context
def activity_export_cmd(
    ctx: click.Context,
    sbc_filter: str | None,
    actor_filter: str | None,
    source_filter: str | None,
    result_filter: str | None,
    since: str | None,
    limit: int,
    export_format: str,
) -> None:
    """Export activity events for downstream processing."""
    manager = _get_manager(ctx)
    events = audit.query_events(
        manager.db,
        limit=limit,
        sbc=sbc_filter,
        actor=actor_filter,
        source=source_filter,
        result=result_filter,
        since=_parse_since(since) if since else None,
        order_desc=False,
    )
    if export_format == "ndjson":
        for event in events:
            click.echo(json.dumps(event, separators=(",", ":")))


@main.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion_cmd(shell: str) -> None:
    """Generate shell completion script.

    To enable completion, add to your shell config:

    \b
    Bash (~/.bashrc):
      eval "$(labctl completion bash)"

    \b
    Zsh (~/.zshrc):
      eval "$(labctl completion zsh)"

    \b
    Fish (~/.config/fish/completions/labctl.fish):
      labctl completion fish > ~/.config/fish/completions/labctl.fish
    """
    import os

    # Click uses environment variables for completion
    shell_complete = {
        "bash": "_LABCTL_COMPLETE=bash_source",
        "zsh": "_LABCTL_COMPLETE=zsh_source",
        "fish": "_LABCTL_COMPLETE=fish_source",
    }

    env_var = shell_complete[shell]
    var_name, var_value = env_var.split("=")

    # Generate completion script by running labctl with the completion env var
    env = os.environ.copy()
    env[var_name] = var_value

    result = subprocess.run(
        ["labctl"],
        env=env,
        capture_output=True,
        text=True,
    )
    click.echo(result.stdout)


# --- User Management ---


@main.group("user")
def user_group():
    """User management commands for authentication."""
    pass


@user_group.command("hash-password")
def hash_password_cmd():
    """Generate a password hash for use in config.

    Prompts for password input and outputs a werkzeug-compatible hash.
    """
    import getpass

    from werkzeug.security import generate_password_hash

    password = getpass.getpass("Password: ")
    if not password:
        click.echo("Error: empty password", err=True)
        raise SystemExit(1)
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        click.echo("Error: passwords do not match", err=True)
        raise SystemExit(1)
    click.echo(generate_password_hash(password))


@user_group.command("generate-key")
def generate_key_cmd():
    """Generate a random API key."""
    import secrets

    click.echo(secrets.token_urlsafe(32))


@user_group.command("add")
@click.argument("username")
def user_add_cmd(username: str):
    """Print a ready-to-paste YAML snippet for a new user.

    Prompts for password, generates API key, outputs YAML.
    """
    import getpass
    import secrets

    from werkzeug.security import generate_password_hash

    password = getpass.getpass("Password: ")
    if not password:
        click.echo("Error: empty password", err=True)
        raise SystemExit(1)
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        click.echo("Error: passwords do not match", err=True)
        raise SystemExit(1)

    pw_hash = generate_password_hash(password)
    api_key = secrets.token_urlsafe(32)

    click.echo("\n# Add this to your config.yaml under auth.users:")
    click.echo(f"    - username: {username}")
    click.echo(f'      password_hash: "{pw_hash}"')
    click.echo(f'      api_key: "{api_key}"')


@user_group.command("verify")
@click.argument("username")
@click.pass_context
def user_verify_cmd(ctx: click.Context, username: str):
    """Verify a password against the config for a user."""
    import getpass

    from werkzeug.security import check_password_hash

    config: Config = ctx.obj["config"]
    if not config.auth.enabled:
        click.echo("Warning: auth is not enabled in config", err=True)

    user = None
    for u in config.auth.users:
        if u.username == username:
            user = u
            break

    if not user:
        click.echo(f"Error: user '{username}' not found in config", err=True)
        raise SystemExit(1)

    password = getpass.getpass("Password: ")
    if check_password_hash(user.password_hash, password):
        click.echo("Password is correct.")
    else:
        click.echo("Password is incorrect.", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
