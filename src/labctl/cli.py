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

# Default paths
LAB_DEV_DIR = Path("/dev/lab")
SER2NET_CONFIG = Path("/etc/ser2net.yaml")


@click.group()
@click.version_option(version=__version__, prog_name="labctl")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Lab Controller - Manage embedded development lab resources."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@main.command("ports")
@click.option(
    "--all", "-a", "show_all", is_flag=True, help="Show all serial devices, not just /dev/lab/"
)
@click.pass_context
def ports_cmd(ctx: click.Context, show_all: bool) -> None:
    """List available serial ports."""
    verbose = ctx.obj.get("verbose", False)

    # Get /dev/lab/ symlinks
    lab_ports = []
    if LAB_DEV_DIR.exists():
        for entry in sorted(LAB_DEV_DIR.iterdir()):
            if entry.is_symlink():
                target = os.readlink(entry)
                # Resolve relative symlinks
                if not target.startswith("/"):
                    target = str((entry.parent / target).resolve())
                lab_ports.append({
                    "name": entry.name,
                    "path": str(entry),
                    "target": target,
                    "tcp_port": _get_tcp_port(entry.name),
                })

    if not lab_ports:
        click.echo("No ports configured in /dev/lab/")
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


def _get_tcp_port(port_name: str) -> int | None:
    """Look up TCP port for a named port from ser2net config."""
    if not SER2NET_CONFIG.exists():
        return None

    try:
        content = SER2NET_CONFIG.read_text()
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

    # Resolve port name to path
    if port_name.startswith("/"):
        port_path = Path(port_name)
        port_name = port_path.name
    else:
        port_path = LAB_DEV_DIR / port_name

    if not port_path.exists():
        click.echo(f"Error: Port not found: {port_path}", err=True)
        sys.exit(1)

    # Look up TCP port
    tcp_port = _get_tcp_port(port_name)

    if tcp_port:
        # Connect via TCP (preferred - allows multiple clients)
        if verbose:
            click.echo(f"Connecting to {port_name} via TCP port {tcp_port}...")
        _connect_tcp("localhost", tcp_port)
    else:
        # Fall back to direct serial connection
        if verbose:
            click.echo(f"Connecting directly to {port_path}...")
        _connect_direct(port_path, baud or 115200)


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


if __name__ == "__main__":
    main()
