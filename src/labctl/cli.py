"""
Command-line interface for lab controller.

Provides commands for managing serial ports, connections, and lab resources.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import click

from labctl import __version__
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
    ctx: click.Context, verbose: bool, quiet: bool, config_path: Path | None, delay: float
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
            click.echo(f"Connecting to alias '{port_name}' via TCP port {port.tcp_port}...")
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
@click.pass_context
def remove_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        click.echo(f"Error: SBC '{name}' not found", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove SBC '{name}' and all associated data?", abort=True)

    if manager.delete_sbc(sbc.id):
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
            f"{d['serial_number']:<25} {d['device_type']:<10} "
            f"{block:<12} {reg_str}"
        )


@sdwire_group.command("add")
@click.argument("name")
@click.argument("serial_number")
@click.option(
    "--type", "device_type",
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
        click.echo(
            f"{d.name:<15} {d.serial_number:<25} {d.device_type:<10} {sbc_name}"
        )


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
@click.pass_context
def sdwire_host_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Switch SD card to host (dev machine can flash it)."""
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
@click.pass_context
def sdwire_flash_cmd(
    ctx: click.Context, sbc_name: str, image: Path, no_reboot: bool
) -> None:
    """Flash an SD card image to an SBC's SDWire.

    Switches to host, writes the image, switches back to DUT,
    and optionally power cycles the SBC.
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

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

    try:
        # Step 1: Switch to host
        click.echo(f"Switching SD card to host...")
        ctrl.switch_to_host()

        import time
        time.sleep(2)  # Wait for block device to appear

        # Step 2: Flash image
        block_dev = ctrl.get_block_device()
        if not block_dev:
            click.echo("Error: Block device not found after switching to host", err=True)
            sys.exit(1)

        click.echo(f"Flashing {image} to {block_dev}...")
        ctrl.flash_image(str(image))
        click.echo("Flash complete")

        # Step 3: Switch back to DUT
        click.echo(f"Switching SD card to DUT...")
        ctrl.switch_to_dut()

        # Step 4: Optional power cycle
        if not no_reboot and sbc.power_plug:
            click.echo(f"Power cycling {sbc_name}...")
            from labctl.power import PowerController
            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_cycle(delay=2.0)
            click.echo(f"Power cycled: {sbc_name}")

        click.echo(f"Done! {sbc_name} should boot from the new image.")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sdwire_group.command("update")
@click.argument("sbc_name")
@click.option(
    "--partition", "-p", type=int, required=True,
    help="Partition number (e.g., 1 for first partition)",
)
@click.option(
    "--copy", "-c", "copies", multiple=True, required=True,
    help="File to copy as source:dest (dest relative to partition root)",
)
@click.option("--reboot", is_flag=True, help="Power cycle the SBC after updating")
@click.pass_context
def sdwire_update_cmd(
    ctx: click.Context,
    sbc_name: str,
    partition: int,
    copies: tuple[str, ...],
    reboot: bool,
) -> None:
    """Copy files to a partition on an SBC's SD card.

    Switches to host, mounts the partition, copies files, unmounts,
    switches back to DUT, and optionally power cycles.

    \b
    Examples:
      labctl sdwire update pi-5 -p 1 --copy kernel.img:kernel_2712.img
      labctl sdwire update pi-5 -p 1 -c fw.bin:firmware.bin -c cfg.txt:config.txt --reboot
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

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

    try:
        # Step 1: Switch to host
        click.echo("Switching SD card to host...")
        ctrl.switch_to_host()

        import time
        time.sleep(2)  # Wait for block device and partitions to appear

        # Step 2: Mount and copy files
        click.echo(f"Mounting partition {partition}...")
        copied = ctrl.update_files(partition, file_pairs)
        for f in copied:
            click.echo(f"  Copied: {f}")

        # Step 3: Switch back to DUT
        click.echo("Switching SD card to DUT...")
        ctrl.switch_to_dut()

        # Step 4: Optional power cycle
        if reboot and sbc.power_plug:
            click.echo(f"Power cycling {sbc_name}...")
            from labctl.power import PowerController
            power_ctrl = PowerController.from_plug(sbc.power_plug)
            power_ctrl.power_cycle(delay=2.0)
            click.echo(f"Power cycled: {sbc_name}")
        elif reboot and not sbc.power_plug:
            click.echo("Warning: --reboot requested but no power plug assigned")

        click.echo("Done!")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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
        click.echo(
            "Run 'labctl serial udev --install' to activate udev rules."
        )
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
        click.echo(
            "Run 'labctl serial udev --install' to update udev rules."
        )
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
        click.echo(
            f"{d.name:<15} {d.usb_path:<15} {vendor:<18} {model:<20} {assigned}"
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
        click.echo(
            "Run 'labctl serial udev --install' to update udev rules."
        )
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
@click.option(
    "--alias", "-a", help="Human-friendly name for this assignment"
)
@click.option(
    "--serial-device", "-s", "serial_device_name",
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


@power_group.command("on")
@click.argument("sbc_name")
@click.pass_context
def power_on_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Turn power on for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Powering on {sbc_name}...")
    try:
        if controller.power_on():
            click.echo(f"Power ON: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power on {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
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
        if controller.power_off():
            click.echo(f"Power OFF: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power off {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
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
        if controller.power_cycle(delay):
            click.echo(f"Power cycled: {sbc_name}")
        else:
            click.echo(f"Error: Failed to power cycle {sbc_name}", err=True)
            sys.exit(1)
    except RuntimeError as e:
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
                        click.echo(
                            f"Connected to localhost:{port.tcp_port}"
                        )

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
                                        line_str = line.decode(
                                            "latin-1"
                                        ).rstrip()

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
                                        click.echo(
                                            f"\nCaptured {line_count} lines"
                                        )
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
                    click.echo(
                        f"Disconnected. Reconnecting in {reconnect_delay}s..."
                    )

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
    import time

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

        click.echo(
            f"{'NAME':<15} {'PROJECT':<12} {'STATUS':<12} {'IP':<15} {'POWER':<10}"
        )
        click.echo("-" * 64)

        for sbc in sbcs:
            color = colors.get(sbc.status, "")
            status_str = f"{color}{sbc.status.value:<12}{reset}"
            ip = sbc.primary_ip or "-"
            project_name = sbc.project or "-"

            # Get power state if plug assigned
            power = "-"
            if sbc.power_plug:
                try:
                    controller = PowerController.from_plug(sbc.power_plug)
                    state = controller.get_state()
                    if state == PowerState.ON:
                        power = f"\033[32mON{reset}"
                    elif state == PowerState.OFF:
                        power = f"\033[31mOFF{reset}"
                    else:
                        power = "?"
                except Exception:
                    power = "err"

            click.echo(
                f"{sbc.name:<15} {project_name:<12} {status_str} {ip:<15} {power:<10}"
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
            click.echo(f"  Warning: Failed to create serial device {sd_name}: {e}", err=True)

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
        click.echo(
            "Error: Both --cert and --key are required for SSL.", err=True
        )
        ctx.exit(1)

    ssl_context = (ssl_cert, ssl_key) if ssl_cert and ssl_key else None
    scheme = "https" if ssl_context else "http"

    click.echo("Starting Lab Controller web server...")
    click.echo(f"Dashboard: {scheme}://{host}:{port}/")
    click.echo(f"API:       {scheme}://{host}:{port}/api/")
    click.echo("Press Ctrl+C to stop")

    app.run(host=host, port=port, debug=debug, ssl_context=ssl_context)


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
