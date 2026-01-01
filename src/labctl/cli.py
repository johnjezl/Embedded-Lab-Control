"""
Command-line interface for lab controller.

Provides commands for managing serial ports, connections, and lab resources.
"""

import os
import subprocess
import sys
from pathlib import Path

import click

from labctl import __version__
from labctl.core.config import Config, load_config
from labctl.core.manager import get_manager, ResourceManager
from labctl.core.models import Status, PortType, AddressType, PlugType
from labctl.serial.ser2net import Ser2NetPort, generate_ser2net_config
from labctl.power import PowerController, PowerState, get_controller


def _get_manager(ctx: click.Context) -> ResourceManager:
    """Get or create resource manager from context."""
    if "manager" not in ctx.obj:
        config: Config = ctx.obj["config"]
        ctx.obj["manager"] = get_manager(config.database_path)
    return ctx.obj["manager"]


@click.group()
@click.version_option(version=__version__, prog_name="labctl")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.option(
    "-c", "--config", "config_path", type=click.Path(exists=True, path_type=Path),
    help="Path to config file"
)
@click.pass_context
def main(ctx: click.Context, verbose: bool, config_path: Path | None) -> None:
    """Lab Controller - Manage embedded development lab resources."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["config"] = load_config(config_path)


@main.command("ports")
@click.option(
    "--all", "-a", "show_all", is_flag=True, help="Show all serial devices, not just /dev/lab/"
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
                lab_ports.append({
                    "name": entry.name,
                    "path": str(entry),
                    "target": target,
                    "tcp_port": _get_tcp_port(entry.name, config),
                })

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

    PORT_NAME is the name of the port (e.g., 'sbc1-console') or the full
    path (e.g., '/dev/lab/sbc1-console').
    """
    verbose = ctx.obj.get("verbose", False)
    config: Config = ctx.obj["config"]
    dev_dir = config.serial.dev_dir

    # Resolve port name to path
    if port_name.startswith("/"):
        port_path = Path(port_name)
        port_name = port_path.name
    else:
        port_path = dev_dir / port_name

    if not port_path.exists():
        click.echo(f"Error: Port not found: {port_path}", err=True)
        sys.exit(1)

    # Look up TCP port
    tcp_port = _get_tcp_port(port_name, config)

    if tcp_port and config.ser2net.enabled:
        # Connect via TCP (preferred - allows multiple clients)
        if verbose:
            click.echo(f"Connecting to {port_name} via TCP port {tcp_port}...")
        _connect_tcp("localhost", tcp_port)
    else:
        # Fall back to direct serial connection
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
@click.option("--status", "-s", type=click.Choice([s.value for s in Status]), help="Filter by status")
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
    click.echo(f"{'NAME':<15} {'PROJECT':<12} {'STATUS':<10} {'CONSOLE':<20} {'IP':<15}")
    click.echo("-" * 72)

    for sbc in sbcs:
        console = "-"
        if sbc.console_port:
            tcp = sbc.console_port.tcp_port
            console = f"tcp:{tcp}" if tcp else sbc.console_port.device_path

        ip = sbc.primary_ip or "-"
        project_name = sbc.project or "-"

        click.echo(f"{sbc.name:<15} {project_name:<12} {sbc.status.value:<10} {console:<20} {ip:<15}")


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
            click.echo(f"  {port.port_type.value}: {port.device_path}{tcp} @ {port.baud_rate}")
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


@main.command("edit")
@click.argument("name")
@click.option("--project", "-p", help="Set project name")
@click.option("--description", "-d", help="Set description")
@click.option("--ssh-user", "-u", help="Set SSH username")
@click.option("--status", "-s", type=click.Choice([s.value for s in Status]), help="Set status")
@click.pass_context
def edit_cmd(
    ctx: click.Context,
    name: str,
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
    if all(v is None for v in [project, description, ssh_user, status]):
        click.echo("No changes specified. Use --project, --description, --ssh-user, or --status.")
        return

    status_enum = Status(status) if status else None

    manager.update_sbc(
        sbc.id,
        project=project,
        description=description,
        ssh_user=ssh_user,
        status=status_enum,
    )
    click.echo(f"Updated SBC: {name}")


# --- Port Assignment Commands ---

@main.group("port")
def port_group() -> None:
    """Manage serial port assignments."""
    pass


@port_group.command("assign")
@click.argument("sbc_name")
@click.argument("port_type", type=click.Choice([t.value for t in PortType]))
@click.argument("device")
@click.option("--tcp-port", "-t", type=int, help="TCP port (auto-assigned if not specified)")
@click.option("--baud", "-b", type=int, default=115200, help="Baud rate (default: 115200)")
@click.pass_context
def port_assign_cmd(
    ctx: click.Context,
    sbc_name: str,
    port_type: str,
    device: str,
    tcp_port: int | None,
    baud: int,
) -> None:
    """Assign a serial port to an SBC."""
    manager = _get_manager(ctx)

    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        click.echo(f"Error: SBC '{sbc_name}' not found", err=True)
        sys.exit(1)

    port = manager.assign_serial_port(
        sbc_id=sbc.id,
        port_type=PortType(port_type),
        device_path=device,
        tcp_port=tcp_port,
        baud_rate=baud,
    )
    click.echo(f"Assigned {port_type} port to {sbc_name}: {device} (tcp:{port.tcp_port})")


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
@click.pass_context
def port_list_cmd(ctx: click.Context) -> None:
    """List all serial port assignments."""
    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

    ports = manager.list_serial_ports()

    if not ports:
        click.echo("No serial ports assigned. Use 'labctl port assign' to assign ports.")
        return

    # Get SBC names for display
    sbc_names = {}
    for sbc in manager.list_sbcs():
        sbc_names[sbc.id] = sbc.name

    click.echo(f"{'SBC':<15} {'TYPE':<10} {'DEVICE':<25} {'TCP':<8} {'BAUD':<10}")
    click.echo("-" * 68)

    for port in ports:
        sbc_name = sbc_names.get(port.sbc_id, f"#{port.sbc_id}")
        tcp = str(port.tcp_port) if port.tcp_port else "-"
        click.echo(f"{sbc_name:<15} {port.port_type.value:<10} {port.device_path:<25} {tcp:<8} {port.baud_rate:<10}")


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

    addr = manager.set_network_address(
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
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Output file (default: stdout)")
@click.option("--install", is_flag=True, help="Install to /etc/ser2net.yaml")
@click.pass_context
def ser2net_generate_cmd(ctx: click.Context, output: Path | None, install: bool) -> None:
    """Generate ser2net configuration from database."""
    manager = _get_manager(ctx)
    config: Config = ctx.obj["config"]

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
        port_name = f"{sbc_name}-{db_port.port_type.value}"

        ports.append(Ser2NetPort(
            name=port_name,
            device=db_port.device_path,
            tcp_port=db_port.tcp_port or 4000,
            baud=db_port.baud_rate,
        ))

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
    try:
        result = subprocess.run(
            ["systemctl", "restart", "ser2net"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.echo("ser2net service restarted successfully")
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
@click.option("--index", "-i", type=int, default=1, help="Outlet index for multi-relay devices (default: 1)")
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

    plug = manager.assign_power_plug(
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
    if controller.power_on():
        click.echo(f"Power ON: {sbc_name}")
    else:
        click.echo(f"Error: Failed to power on {sbc_name}", err=True)
        sys.exit(1)


@power_group.command("off")
@click.argument("sbc_name")
@click.pass_context
def power_off_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Turn power off for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Powering off {sbc_name}...")
    if controller.power_off():
        click.echo(f"Power OFF: {sbc_name}")
    else:
        click.echo(f"Error: Failed to power off {sbc_name}", err=True)
        sys.exit(1)


@power_group.command("cycle")
@click.argument("sbc_name")
@click.option("--delay", "-d", type=float, default=2.0, help="Delay between off and on (default: 2s)")
@click.pass_context
def power_cycle_cmd(ctx: click.Context, sbc_name: str, delay: float) -> None:
    """Power cycle an SBC (off, wait, on)."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    click.echo(f"Power cycling {sbc_name} (delay: {delay}s)...")
    if controller.power_cycle(delay):
        click.echo(f"Power cycled: {sbc_name}")
    else:
        click.echo(f"Error: Failed to power cycle {sbc_name}", err=True)
        sys.exit(1)


@power_group.command("status")
@click.argument("sbc_name")
@click.pass_context
def power_status_cmd(ctx: click.Context, sbc_name: str) -> None:
    """Show power status for an SBC."""
    manager = _get_manager(ctx)
    controller, sbc = _get_power_controller(manager, sbc_name)

    state = controller.get_state()
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
def power_all_cmd(ctx: click.Context, action: str, project: str | None, yes: bool) -> None:
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
        click.echo(f"  - {sbc.name} ({sbc.power_plug.plug_type.value} @ {sbc.power_plug.address})")

    if not yes:
        click.confirm(f"\nPower {action.upper()} all {len(sbcs_with_plugs)} SBC(s)?", abort=True)

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

    click.echo(f"\n{success_count}/{len(sbcs_with_plugs)} SBCs powered {action.upper()}")


# --- Console and SSH Commands ---

@main.command("console")
@click.argument("sbc_name")
@click.option("--type", "-t", "port_type", type=click.Choice([t.value for t in PortType]),
              default="console", help="Port type (default: console)")
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
@click.pass_context
def status_cmd(ctx: click.Context, project: str | None) -> None:
    """Show status overview of all SBCs."""
    manager = _get_manager(ctx)

    sbcs = manager.list_sbcs(project=project)

    if not sbcs:
        filter_msg = f" in project '{project}'" if project else ""
        click.echo(f"No SBCs configured{filter_msg}.")
        return

    # Status colors (ANSI)
    colors = {
        Status.ONLINE: "\033[32m",   # Green
        Status.OFFLINE: "\033[31m",  # Red
        Status.BOOTING: "\033[33m",  # Yellow
        Status.ERROR: "\033[31m",    # Red
        Status.UNKNOWN: "\033[90m",  # Gray
    }
    reset = "\033[0m"

    click.echo(f"{'NAME':<15} {'PROJECT':<12} {'STATUS':<12} {'IP':<15} {'POWER':<10}")
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

        click.echo(f"{sbc.name:<15} {project_name:<12} {status_str} {ip:<15} {power:<10}")


# --- Export/Import Commands ---

@main.command("export")
@click.option("--format", "-f", "fmt", type=click.Choice(["yaml", "json"]),
              default="yaml", help="Output format (default: yaml)")
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Output file (default: stdout)")
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

    # Build export data
    data = {"sbcs": []}
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
                sbc_data["serial_ports"].append({
                    "type": port.port_type.value,
                    "device": port.device_path,
                    "tcp_port": port.tcp_port,
                    "baud_rate": port.baud_rate,
                })

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
@click.option("--update", "-u", is_flag=True, help="Update existing SBCs instead of skipping")
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
            manager.assign_serial_port(
                sbc_id=sbc.id,
                port_type=PortType(port_data["type"]),
                device_path=port_data["device"],
                tcp_port=port_data.get("tcp_port"),
                baud_rate=port_data.get("baud_rate", 115200),
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

    click.echo(f"\nImport complete: {created} created, {updated} updated, {skipped} skipped")


# --- Web Server ---

@main.command("web")
@click.option("--host", "-h", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", "-p", type=int, default=5000, help="Port to bind to (default: 5000)")
@click.option("--debug", is_flag=True, help="Enable debug mode")
@click.pass_context
def web_cmd(ctx: click.Context, host: str, port: int, debug: bool) -> None:
    """Start the web dashboard server."""
    from labctl.web import create_app

    config: Config = ctx.obj["config"]
    app = create_app(config)

    click.echo(f"Starting Lab Controller web server...")
    click.echo(f"Dashboard: http://{host}:{port}/")
    click.echo(f"API:       http://{host}:{port}/api/")
    click.echo("Press Ctrl+C to stop")

    app.run(host=host, port=port, debug=debug)


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


if __name__ == "__main__":
    main()
