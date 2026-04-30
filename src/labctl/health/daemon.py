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
from labctl.health.alerts import Alert, AlertLevel, AlertManager
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
        interval: int = 10,
        power_check_interval: int = 60,
        min_sleep_seconds: float = 1.0,
        update_status: bool = True,
        alert_on_offline: bool = True,
        alert_on_power_change: bool = True,
    ):
        """
        Initialize monitoring daemon.

        The daemon runs two cadences:

          - Fast track (`interval`, default 10 s): ping + serial probe.
            These are cheap localhost / ICMP operations, safe to run
            frequently.
          - Slow track (`power_check_interval`, default 60 s): power
            probe. Network-bound and sometimes slow (Kasa KLAP retries
            can take seconds), so it runs less often. Power is included
            on every Nth fast tick where N = ceil(power_check_interval
            / interval).

        Args:
            manager: Resource manager for SBC access
            checker: Health checker instance
            alert_manager: Alert manager for notifications
            interval: Fast-track cycle interval (ping + serial), seconds
            power_check_interval: Slow-track cadence (power), seconds
            min_sleep_seconds: Floor on between-cycle sleep so a runaway
                cycle (elapsed >= interval) can't pin a CPU spinning.
            update_status: Whether to update SBC status in database
            alert_on_offline: Alert when SBC goes offline
            alert_on_power_change: Alert on power state changes
        """
        self.manager = manager
        self.checker = checker
        self.alert_manager = alert_manager
        self.interval = interval
        self.power_check_interval = power_check_interval
        self.min_sleep_seconds = min_sleep_seconds
        self.update_status = update_status
        self.alert_on_offline = alert_on_offline
        self.alert_on_power_change = alert_on_power_change

        self._running = False
        self._last_status: dict[str, Status] = {}
        self._last_power: dict[str, str] = {}
        self._last_power_check: float = 0.0  # monotonic time of last power probe

    def _should_check_power(self, now_monotonic: float) -> bool:
        """Return True if this tick should include the power probe."""
        return (now_monotonic - self._last_power_check) >= self.power_check_interval

    def run_once(self, include_power: Optional[bool] = None) -> dict[str, HealthCheckSummary]:
        """
        Run a single health check pass.

        Args:
            include_power: When True, probe power; when False, skip it
                (cheap fast-track cycle). When None, decide based on
                elapsed time since the last power probe.

        Returns:
            Dictionary of SBC names to health check summaries.
        """
        from labctl.health.checks import CheckType

        if include_power is None:
            include_power = self._should_check_power(time.monotonic())

        check_types = [CheckType.PING, CheckType.SERIAL]
        if include_power:
            check_types.append(CheckType.POWER)

        sbcs = self.manager.list_sbcs()
        results = self.checker.check_all(sbcs, check_types=check_types)

        if include_power:
            self._last_power_check = time.monotonic()

        for sbc_name, summary in results.items():
            self._process_result(sbc_name, summary)

        return results

    def _process_result(self, sbc_name: str, summary: HealthCheckSummary) -> None:
        """Process a health check result and trigger alerts if needed."""
        new_status = summary.recommended_status
        old_status = self._last_status.get(sbc_name)

        logger.info(
            "Health check %s: ping=%s serial=%s power=%s -> %s",
            sbc_name,
            summary.ping_result.success if summary.ping_result else "n/a",
            summary.serial_result.success if summary.serial_result else "n/a",
            summary.power_state.value if summary.power_state else "n/a",
            new_status.value if new_status else "n/a",
        )

        # Update status in database if enabled
        sbc = None
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

        # Cache the power observation so `labctl status --fast` can read
        # it without making live network calls. Stamp every cycle (even
        # when the value is unchanged) so freshness is meaningful.
        if self.update_status and summary.power_state is not None:
            if sbc is None:
                sbc = self.manager.get_sbc_by_name(sbc_name)
            if sbc is not None:
                try:
                    self.manager.update_power_observation(
                        sbc.id, summary.power_state.value
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "Failed to cache power observation for %s: %s", sbc_name, e
                    )

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
                    Alert(
                        level=level,
                        sbc_name=sbc_name,
                        message=f"Power changed to {power_str.upper()}",
                        details=f"Previous: {old_power.upper()}",
                    )
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

        logger.info(
            "Starting monitor daemon: fast=%ds, power=%ds, min_sleep=%.1fs",
            self.interval,
            self.power_check_interval,
            self.min_sleep_seconds,
        )

        # Initialize last known states
        sbcs = self.manager.list_sbcs()
        for sbc in sbcs:
            self._last_status[sbc.name] = sbc.status

        while self._running:
            try:
                start_time = time.monotonic()
                results = self.run_once()
                elapsed = time.monotonic() - start_time

                logger.debug(
                    f"Health check completed for {len(results)} SBCs "
                    f"in {elapsed:.2f}s"
                )

                # Sleep until the next tick. Apply min_sleep_seconds floor
                # so a cycle that overruns the interval can't pin a CPU
                # in a tight loop.
                sleep_time = max(self.min_sleep_seconds, self.interval - elapsed)
                if self._running:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error during health check: {e}")
                if self._running:
                    time.sleep(max(self.min_sleep_seconds, self.interval))

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
