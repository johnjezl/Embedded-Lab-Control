"""
Monitoring daemon for lab controller.

Runs periodic health checks and triggers alerts on status changes.
"""

import logging
import signal
import time
from typing import Optional

from labctl.core.manager import ResourceManager
from labctl.core.models import Status
from labctl.health.alerts import AlertLevel, AlertManager
from labctl.health.checks import HealthChecker, HealthCheckSummary

logger = logging.getLogger(__name__)


class MonitorDaemon:
    """
    Monitoring daemon that runs periodic health checks.

    Tracks status changes and triggers alerts when SBC states change.
    """

    def __init__(
        self,
        manager: ResourceManager,
        checker: HealthChecker,
        alert_manager: AlertManager,
        interval: int = 60,
        update_status: bool = True,
        alert_on_offline: bool = True,
        alert_on_power_change: bool = True,
    ):
        """
        Initialize monitoring daemon.

        Args:
            manager: Resource manager for SBC access
            checker: Health checker instance
            alert_manager: Alert manager for notifications
            interval: Check interval in seconds
            update_status: Whether to update SBC status in database
            alert_on_offline: Alert when SBC goes offline
            alert_on_power_change: Alert on power state changes
        """
        self.manager = manager
        self.checker = checker
        self.alert_manager = alert_manager
        self.interval = interval
        self.update_status = update_status
        self.alert_on_offline = alert_on_offline
        self.alert_on_power_change = alert_on_power_change

        self._running = False
        self._last_status: dict[str, Status] = {}
        self._last_power: dict[str, str] = {}

    def run_once(self) -> dict[str, HealthCheckSummary]:
        """
        Run a single health check pass.

        Returns:
            Dictionary of SBC names to health check summaries
        """
        sbcs = self.manager.list_sbcs()
        results = self.checker.check_all(sbcs)

        for sbc_name, summary in results.items():
            self._process_result(sbc_name, summary)

        return results

    def _process_result(self, sbc_name: str, summary: HealthCheckSummary) -> None:
        """Process a health check result and trigger alerts if needed."""
        new_status = summary.recommended_status
        old_status = self._last_status.get(sbc_name)

        # Update status in database if enabled
        if self.update_status and new_status:
            sbc = self.manager.get_sbc_by_name(sbc_name)
            if sbc and sbc.status != new_status:
                # Build details string
                details_parts = []
                if summary.ping_result:
                    details_parts.append(summary.ping_result.message)
                if summary.serial_result:
                    details_parts.append(summary.serial_result.message)
                if summary.power_result:
                    details_parts.append(summary.power_result.message)
                details = "; ".join(details_parts) if details_parts else None

                self.manager.update_sbc(sbc.id, status=new_status)
                self.manager.log_status(sbc.id, new_status, details)

        # Check for status change alerts
        if self.alert_on_offline and new_status:
            if old_status and old_status != new_status:
                if new_status == Status.OFFLINE:
                    self.alert_manager.trigger_critical(
                        sbc_name,
                        "SBC went OFFLINE",
                        f"Previous status: {old_status.value}",
                    )
                elif new_status == Status.ONLINE and old_status == Status.OFFLINE:
                    self.alert_manager.trigger_info(
                        sbc_name,
                        "SBC came ONLINE",
                        f"Previous status: {old_status.value}",
                    )
                elif new_status == Status.ERROR:
                    self.alert_manager.trigger_warning(
                        sbc_name,
                        "SBC in ERROR state",
                        (
                            summary.serial_result.message
                            if summary.serial_result
                            else None
                        ),
                    )

        # Check for power state changes
        if self.alert_on_power_change and summary.power_state:
            power_str = summary.power_state.value
            old_power = self._last_power.get(sbc_name)

            if old_power and old_power != power_str:
                level = AlertLevel.WARNING if power_str == "off" else AlertLevel.INFO
                self.alert_manager.trigger(
                    level=level,
                    sbc_name=sbc_name,
                    message=f"Power changed to {power_str.upper()}",
                    details=f"Previous: {old_power.upper()}",
                )
            self._last_power[sbc_name] = power_str

        # Update last known status
        if new_status:
            self._last_status[sbc_name] = new_status

    def start(self) -> None:
        """
        Start the monitoring daemon.

        Runs until stop() is called or SIGTERM/SIGINT is received.
        """
        self._running = True

        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, stopping daemon...")
            self.stop()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        logger.info(f"Starting monitor daemon with {self.interval}s interval")

        # Initialize last known states
        sbcs = self.manager.list_sbcs()
        for sbc in sbcs:
            self._last_status[sbc.name] = sbc.status

        while self._running:
            try:
                start_time = time.time()
                results = self.run_once()
                elapsed = time.time() - start_time

                logger.debug(
                    f"Health check completed for {len(results)} SBCs "
                    f"in {elapsed:.2f}s"
                )

                # Sleep for remaining interval time
                sleep_time = max(0, self.interval - elapsed)
                if sleep_time > 0 and self._running:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error during health check: {e}")
                if self._running:
                    time.sleep(self.interval)

        logger.info("Monitor daemon stopped")

    def stop(self) -> None:
        """Stop the monitoring daemon."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if daemon is running."""
        return self._running


def format_check_table(
    results: dict[str, HealthCheckSummary],
    show_details: bool = False,
) -> str:
    """
    Format health check results as a table.

    Args:
        results: Dictionary of SBC names to check summaries
        show_details: Whether to show detailed messages

    Returns:
        Formatted table string
    """
    if not results:
        return "No SBCs to check."

    # Header
    lines = []
    header = f"{'SBC':<20} {'PING':<8} {'SERIAL':<8} {'POWER':<8} {'STATUS':<10}"
    lines.append(header)
    lines.append("-" * len(header))

    # Rows
    for sbc_name, summary in sorted(results.items()):
        ping = _format_check(summary.ping_result)
        serial = _format_check(summary.serial_result)
        power = _format_power(summary.power_result, summary.power_state)
        status = summary.recommended_status.value if summary.recommended_status else "-"

        lines.append(f"{sbc_name:<20} {ping:<8} {serial:<8} {power:<8} {status:<10}")

        if show_details:
            if summary.ping_result:
                lines.append(f"  Ping: {summary.ping_result.message}")
            if summary.serial_result:
                lines.append(f"  Serial: {summary.serial_result.message}")
            if summary.power_result:
                lines.append(f"  Power: {summary.power_result.message}")

    return "\n".join(lines)


def _format_check(result: Optional[object]) -> str:
    """Format a check result for table display."""
    if result is None:
        return "-"
    return "\u2713" if result.success else "\u2717"


def _format_power(
    result: Optional[object],
    state: Optional[object],
) -> str:
    """Format power check result for table display."""
    if result is None:
        return "-"
    if state:
        return state.value.upper()
    return "\u2717" if not result.success else "?"
