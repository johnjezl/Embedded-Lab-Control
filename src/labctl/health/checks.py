"""
Health check implementations for lab controller.

Provides ping, serial, and power health checks for SBCs.
"""

import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from labctl.core.models import SBC, PortType, Status
from labctl.power.base import PowerController, PowerState, get_controller


class CheckType(Enum):
    """Types of health checks available."""

    PING = "ping"
    SERIAL = "serial"
    POWER = "power"


@dataclass
class CheckResult:
    """Result of a single health check."""

    sbc_name: str
    check_type: CheckType
    success: bool
    message: str
    duration_ms: float
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def status_char(self) -> str:
        """Get single character status indicator."""
        if self.success:
            return "\u2713"  # checkmark
        return "\u2717"  # X mark


@dataclass
class HealthCheckSummary:
    """Summary of health checks for an SBC."""

    sbc_name: str
    ping_result: Optional[CheckResult] = None
    serial_result: Optional[CheckResult] = None
    power_result: Optional[CheckResult] = None
    power_state: Optional[PowerState] = None
    recommended_status: Optional[Status] = None

    def determine_status(self) -> Status:
        """Determine recommended SBC status based on check results."""
        # If ping fails, SBC is offline
        if self.ping_result and not self.ping_result.success:
            return Status.OFFLINE

        # If power is off, SBC is offline
        if self.power_state == PowerState.OFF:
            return Status.OFFLINE

        # If ping succeeds but serial fails, there's an error
        if self.ping_result and self.ping_result.success:
            if self.serial_result and not self.serial_result.success:
                return Status.ERROR

        # If ping succeeds, SBC is online
        if self.ping_result and self.ping_result.success:
            return Status.ONLINE

        # Can't determine status
        return Status.UNKNOWN


class HealthChecker:
    """Performs health checks on SBCs."""

    def __init__(
        self,
        ping_timeout: float = 2.0,
        serial_timeout: float = 2.0,
    ):
        """
        Initialize health checker.

        Args:
            ping_timeout: Timeout for ping checks in seconds
            serial_timeout: Timeout for serial port checks in seconds
        """
        self.ping_timeout = ping_timeout
        self.serial_timeout = serial_timeout

    def ping_check(self, ip: str, sbc_name: str = "") -> CheckResult:
        """
        Check if an IP address responds to ICMP ping.

        Args:
            ip: IP address to ping
            sbc_name: Name of SBC for result tracking

        Returns:
            CheckResult with success/failure status
        """
        start_time = time.time()

        try:
            # Use ping command with 1 packet and timeout
            # -c 1: send 1 packet
            # -W: timeout in seconds (Linux)
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(int(self.ping_timeout)), ip],
                capture_output=True,
                timeout=self.ping_timeout + 1,
            )
            duration_ms = (time.time() - start_time) * 1000

            if result.returncode == 0:
                return CheckResult(
                    sbc_name=sbc_name,
                    check_type=CheckType.PING,
                    success=True,
                    message=f"Host {ip} is reachable",
                    duration_ms=duration_ms,
                )
            else:
                return CheckResult(
                    sbc_name=sbc_name,
                    check_type=CheckType.PING,
                    success=False,
                    message=f"Host {ip} is unreachable",
                    duration_ms=duration_ms,
                )

        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.PING,
                success=False,
                message=f"Ping to {ip} timed out",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.PING,
                success=False,
                message=f"Ping error: {e}",
                duration_ms=duration_ms,
            )

    def serial_check(self, host: str, port: int, sbc_name: str = "") -> CheckResult:
        """
        Check if a serial port (via TCP) is accessible.

        Args:
            host: Host address (usually localhost for ser2net)
            port: TCP port number
            sbc_name: Name of SBC for result tracking

        Returns:
            CheckResult with success/failure status
        """
        start_time = time.time()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.serial_timeout)
            sock.connect((host, port))
            sock.close()

            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.SERIAL,
                success=True,
                message=f"Serial port {host}:{port} is accessible",
                duration_ms=duration_ms,
            )

        except socket.timeout:
            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.SERIAL,
                success=False,
                message=f"Connection to {host}:{port} timed out",
                duration_ms=duration_ms,
            )
        except ConnectionRefusedError:
            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.SERIAL,
                success=False,
                message=f"Connection to {host}:{port} refused",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return CheckResult(
                sbc_name=sbc_name,
                check_type=CheckType.SERIAL,
                success=False,
                message=f"Serial check error: {e}",
                duration_ms=duration_ms,
            )

    def power_check(
        self, controller: PowerController, sbc_name: str = ""
    ) -> tuple[CheckResult, PowerState]:
        """
        Check power state of an SBC via its power controller.

        Args:
            controller: Power controller instance
            sbc_name: Name of SBC for result tracking

        Returns:
            Tuple of (CheckResult, PowerState)
        """
        start_time = time.time()

        try:
            state = controller.get_state()
            duration_ms = (time.time() - start_time) * 1000

            if state == PowerState.UNKNOWN:
                return (
                    CheckResult(
                        sbc_name=sbc_name,
                        check_type=CheckType.POWER,
                        success=False,
                        message="Power state unknown",
                        duration_ms=duration_ms,
                    ),
                    state,
                )
            else:
                return (
                    CheckResult(
                        sbc_name=sbc_name,
                        check_type=CheckType.POWER,
                        success=True,
                        message=f"Power is {state.value.upper()}",
                        duration_ms=duration_ms,
                    ),
                    state,
                )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return (
                CheckResult(
                    sbc_name=sbc_name,
                    check_type=CheckType.POWER,
                    success=False,
                    message=f"Power check error: {e}",
                    duration_ms=duration_ms,
                ),
                PowerState.UNKNOWN,
            )

    def check_sbc(
        self,
        sbc: SBC,
        check_types: Optional[list[CheckType]] = None,
    ) -> HealthCheckSummary:
        """
        Run health checks on a single SBC.

        Args:
            sbc: SBC to check
            check_types: List of check types to run (default: all applicable)

        Returns:
            HealthCheckSummary with all check results
        """
        if check_types is None:
            check_types = [CheckType.PING, CheckType.SERIAL, CheckType.POWER]

        summary = HealthCheckSummary(sbc_name=sbc.name)

        # Ping check - requires IP address
        if CheckType.PING in check_types and sbc.primary_ip:
            summary.ping_result = self.ping_check(sbc.primary_ip, sbc.name)

        # Serial check - requires console port with TCP
        if CheckType.SERIAL in check_types and sbc.serial_ports:
            console_port = next(
                (p for p in sbc.serial_ports if p.port_type == PortType.CONSOLE),
                None,
            )
            if console_port and console_port.tcp_port:
                summary.serial_result = self.serial_check(
                    "localhost", console_port.tcp_port, sbc.name
                )

        # Power check - requires power plug
        if CheckType.POWER in check_types and sbc.power_plug:
            controller = get_controller(sbc.power_plug)
            result, state = self.power_check(controller, sbc.name)
            summary.power_result = result
            summary.power_state = state

        # Determine recommended status
        summary.recommended_status = summary.determine_status()

        return summary

    def check_all(
        self,
        sbcs: list[SBC],
        check_types: Optional[list[CheckType]] = None,
    ) -> dict[str, HealthCheckSummary]:
        """
        Run health checks on multiple SBCs.

        Args:
            sbcs: List of SBCs to check
            check_types: List of check types to run (default: all applicable)

        Returns:
            Dictionary mapping SBC names to their check summaries
        """
        results = {}
        for sbc in sbcs:
            results[sbc.name] = self.check_sbc(sbc, check_types)
        return results
