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
from labctl.core.models import Status, PortType


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


if __name__ == "__main__":
    main()
