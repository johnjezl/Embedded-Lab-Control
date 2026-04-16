"""
Serial port capture and send utilities.

Provides functions to capture serial output and send data via ser2net TCP
connections. Used by CLI commands and MCP tools.
"""

import logging
import re
import select
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CaptureResult:
    """Result of a serial capture operation."""

    output: str = ""
    lines: int = 0
    pattern_matched: bool = False
    elapsed_seconds: float = 0.0

    def to_mcp_string(self, pattern: Optional[str] = None) -> str:
        """Format as MCP tool return string."""
        status = f"pattern '{pattern}' matched" if self.pattern_matched else "timeout"
        header = (
            f"[Captured {self.lines} lines in {self.elapsed_seconds:.1f}s, "
            f"{status}]"
        )
        return f"{header}\n{self.output}"


@dataclass
class SendResult:
    """Result of a serial send operation."""

    sent: bool = False
    bytes_sent: int = 0
    capture: Optional[CaptureResult] = None

    def to_mcp_string(self) -> str:
        """Format as MCP tool return string."""
        if self.capture:
            return self.capture.to_mcp_string()
        return f"Sent {self.bytes_sent} bytes"


def resolve_port(manager, port_name: str):
    """Resolve a port name to a SerialPort object.

    Tries alias first, then falls back to SBC name + console port.

    Args:
        manager: ResourceManager instance
        port_name: Port alias or SBC name

    Returns:
        SerialPort object

    Raises:
        ValueError: If port cannot be resolved
    """
    # Try alias first
    port = manager.get_serial_port_by_alias(port_name)
    if port:
        return port

    # Fall back to SBC name -> console port
    sbc = manager.get_sbc_by_name(port_name)
    if sbc:
        if sbc.console_port:
            return sbc.console_port
        raise ValueError(f"SBC '{port_name}' has no console port assigned")

    raise ValueError(f"'{port_name}' is not a known port alias or SBC name")


def capture_serial_output(
    tcp_host: str,
    tcp_port: int,
    timeout: float = 15.0,
    until_pattern: Optional[str] = None,
    tail: Optional[int] = None,
) -> CaptureResult:
    """Capture serial output from a ser2net TCP port.

    Connects to the ser2net TCP port, reads output until timeout or
    pattern match, then disconnects.

    Args:
        tcp_host: Host where ser2net is running (usually "localhost")
        tcp_port: TCP port for the serial connection
        timeout: Maximum seconds to capture
        until_pattern: Regex pattern to stop on (matched per line)
        tail: Return only last N lines (None = all)

    Returns:
        CaptureResult with captured output and metadata.
    """
    compiled_pattern = None
    if until_pattern:
        compiled_pattern = re.compile(until_pattern)

    captured_lines: list[str] = []
    pattern_matched = False
    start_time = time.monotonic()
    buffer = b""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)  # Connection timeout

    try:
        sock.connect((tcp_host, tcp_port))
        sock.setblocking(False)

        while True:
            elapsed = time.monotonic() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                break

            wait = min(remaining, 0.5)
            readable, _, _ = select.select([sock], [], [], wait)

            if readable:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break  # Connection closed

                    buffer += data

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line_str = line.decode("utf-8", errors="replace").rstrip()
                        captured_lines.append(line_str)

                        if compiled_pattern and compiled_pattern.search(line_str):
                            pattern_matched = True
                            break

                    # Check partial buffer for pattern (e.g. prompts without newline)
                    if not pattern_matched and compiled_pattern and buffer:
                        partial = buffer.decode("utf-8", errors="replace")
                        if compiled_pattern.search(partial):
                            captured_lines.append(partial.rstrip())
                            buffer = b""
                            pattern_matched = True

                    if pattern_matched:
                        break

                except BlockingIOError:
                    pass

        # Handle any remaining data in buffer (no trailing newline)
        if buffer:
            line_str = buffer.decode("utf-8", errors="replace").rstrip()
            if line_str:
                captured_lines.append(line_str)
                if compiled_pattern and compiled_pattern.search(line_str):
                    pattern_matched = True

    except (ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            f"Cannot connect to serial port at {tcp_host}:{tcp_port}: {e}"
        )
    finally:
        try:
            sock.close()
        except Exception:
            pass

    elapsed = time.monotonic() - start_time

    if tail and len(captured_lines) > tail:
        output_lines = captured_lines[-tail:]
    else:
        output_lines = captured_lines

    return CaptureResult(
        output="\n".join(output_lines),
        lines=len(output_lines),
        pattern_matched=pattern_matched,
        elapsed_seconds=round(elapsed, 1),
    )


def send_serial_data(
    tcp_host: str,
    tcp_port: int,
    data: str,
    newline: bool = True,
    capture_timeout: Optional[float] = None,
    capture_until: Optional[str] = None,
) -> SendResult:
    """Send data to a serial port via ser2net TCP.

    Optionally captures the response after sending.

    Args:
        tcp_host: Host where ser2net is running
        tcp_port: TCP port for the serial connection
        data: String data to send
        newline: Append \\r\\n after data (default True)
        capture_timeout: If set, capture response for this many seconds
        capture_until: If set, capture until this regex matches

    Returns:
        SendResult with send status and optional capture.
    """
    payload = data
    if newline:
        payload += "\r\n"
    raw = payload.encode("utf-8")

    if capture_timeout is not None:
        # Send and capture in one connection
        return _send_and_capture(
            tcp_host,
            tcp_port,
            raw,
            capture_timeout,
            capture_until,
        )

    # Send only
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)

    try:
        sock.connect((tcp_host, tcp_port))
        sock.sendall(raw)
        return SendResult(sent=True, bytes_sent=len(raw))
    except (ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            f"Cannot connect to serial port at {tcp_host}:{tcp_port}: {e}"
        )
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _send_and_capture(
    tcp_host: str,
    tcp_port: int,
    raw_data: bytes,
    timeout: float,
    until_pattern: Optional[str] = None,
) -> SendResult:
    """Send data then capture response on the same connection."""
    compiled_pattern = None
    if until_pattern:
        compiled_pattern = re.compile(until_pattern)

    captured_lines: list[str] = []
    pattern_matched = False
    buffer = b""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)

    try:
        sock.connect((tcp_host, tcp_port))

        # Drain stale data (100ms)
        sock.setblocking(False)
        drain_end = time.monotonic() + 0.1
        while time.monotonic() < drain_end:
            readable, _, _ = select.select([sock], [], [], 0.05)
            if readable:
                try:
                    sock.recv(4096)
                except BlockingIOError:
                    pass

        # Send data
        sock.setblocking(True)
        sock.settimeout(5.0)
        sock.sendall(raw_data)
        bytes_sent = len(raw_data)

        # Capture response
        sock.setblocking(False)
        start_time = time.monotonic()

        while True:
            elapsed = time.monotonic() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                break

            wait = min(remaining, 0.5)
            readable, _, _ = select.select([sock], [], [], wait)

            if readable:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break

                    buffer += data

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line_str = line.decode("utf-8", errors="replace").rstrip()
                        captured_lines.append(line_str)

                        if compiled_pattern and compiled_pattern.search(line_str):
                            pattern_matched = True
                            break

                    # Check partial buffer for pattern (e.g. prompts without newline)
                    if not pattern_matched and compiled_pattern and buffer:
                        partial = buffer.decode("utf-8", errors="replace")
                        if compiled_pattern.search(partial):
                            captured_lines.append(partial.rstrip())
                            buffer = b""
                            pattern_matched = True

                    if pattern_matched:
                        break

                except BlockingIOError:
                    pass

        # Remaining buffer
        if buffer:
            line_str = buffer.decode("utf-8", errors="replace").rstrip()
            if line_str:
                captured_lines.append(line_str)
                if compiled_pattern and compiled_pattern.search(line_str):
                    pattern_matched = True

        elapsed = time.monotonic() - start_time

        capture = CaptureResult(
            output="\n".join(captured_lines),
            lines=len(captured_lines),
            pattern_matched=pattern_matched,
            elapsed_seconds=round(elapsed, 1),
        )

        return SendResult(sent=True, bytes_sent=bytes_sent, capture=capture)

    except (ConnectionRefusedError, OSError) as e:
        raise RuntimeError(
            f"Cannot connect to serial port at {tcp_host}:{tcp_port}: {e}"
        )
    finally:
        try:
            sock.close()
        except Exception:
            pass
