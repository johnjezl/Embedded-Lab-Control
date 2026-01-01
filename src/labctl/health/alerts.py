"""
Alerting framework for lab controller.

Provides alert handling with pluggable handlers for different notification channels.
Currently implements log file output; email and Slack are stubs for future.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """An alert to be sent to handlers."""

    level: AlertLevel
    sbc_name: str
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    details: Optional[str] = None

    def format(self) -> str:
        """Format alert as a string."""
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        level = self.level.value.upper()
        msg = f"[{ts}] [{level}] {self.sbc_name}: {self.message}"
        if self.details:
            msg += f" - {self.details}"
        return msg


class AlertHandler(ABC):
    """Abstract base class for alert handlers."""

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """
        Send an alert.

        Args:
            alert: Alert to send

        Returns:
            True if alert was sent successfully
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Clean up handler resources."""
        pass


class LogAlertHandler(AlertHandler):
    """Alert handler that writes to a log file."""

    def __init__(self, log_path: Path):
        """
        Initialize log alert handler.

        Args:
            log_path: Path to the alert log file
        """
        self.log_path = log_path
        self._file_handle: Optional[object] = None
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        """Ensure the log directory exists."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, alert: Alert) -> bool:
        """Write alert to log file."""
        try:
            with open(self.log_path, "a") as f:
                f.write(alert.format() + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to write alert to log: {e}")
            return False

    def close(self) -> None:
        """No cleanup needed for file-based logging."""
        pass


class ConsoleAlertHandler(AlertHandler):
    """Alert handler that prints to console."""

    def __init__(self, min_level: AlertLevel = AlertLevel.INFO):
        """
        Initialize console alert handler.

        Args:
            min_level: Minimum alert level to display
        """
        self.min_level = min_level
        self._level_order = {
            AlertLevel.INFO: 0,
            AlertLevel.WARNING: 1,
            AlertLevel.CRITICAL: 2,
        }

    def send(self, alert: Alert) -> bool:
        """Print alert to console if level meets threshold."""
        if self._level_order[alert.level] >= self._level_order[self.min_level]:
            # Color codes for different levels
            colors = {
                AlertLevel.INFO: "\033[32m",  # Green
                AlertLevel.WARNING: "\033[33m",  # Yellow
                AlertLevel.CRITICAL: "\033[31m",  # Red
            }
            reset = "\033[0m"
            color = colors.get(alert.level, "")
            print(f"{color}{alert.format()}{reset}")
        return True

    def close(self) -> None:
        """No cleanup needed for console output."""
        pass


class EmailAlertHandler(AlertHandler):
    """
    Placeholder for email alert handler.

    This is a stub for future implementation.
    """

    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        sender: str = "",
        recipients: Optional[list[str]] = None,
    ):
        """Initialize email handler (stub)."""
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.recipients = recipients or []
        logger.warning("EmailAlertHandler is a stub - emails will not be sent")

    def send(self, alert: Alert) -> bool:
        """Stub: Log that email would be sent."""
        logger.debug(
            f"Email alert (stub): Would send to {self.recipients}: {alert.format()}"
        )
        return False  # Return False since we didn't actually send

    def close(self) -> None:
        """No cleanup needed for stub."""
        pass


class SlackAlertHandler(AlertHandler):
    """
    Placeholder for Slack webhook alert handler.

    This is a stub for future implementation.
    """

    def __init__(self, webhook_url: str = ""):
        """Initialize Slack handler (stub)."""
        self.webhook_url = webhook_url
        logger.warning("SlackAlertHandler is a stub - Slack messages will not be sent")

    def send(self, alert: Alert) -> bool:
        """Stub: Log that Slack message would be sent."""
        logger.debug(f"Slack alert (stub): Would post to webhook: {alert.format()}")
        return False  # Return False since we didn't actually send

    def close(self) -> None:
        """No cleanup needed for stub."""
        pass


class AlertManager:
    """Manages multiple alert handlers and dispatches alerts."""

    def __init__(self):
        """Initialize alert manager."""
        self._handlers: list[AlertHandler] = []

    def add_handler(self, handler: AlertHandler) -> None:
        """
        Register an alert handler.

        Args:
            handler: Handler to add
        """
        self._handlers.append(handler)

    def remove_handler(self, handler: AlertHandler) -> None:
        """
        Remove an alert handler.

        Args:
            handler: Handler to remove
        """
        if handler in self._handlers:
            self._handlers.remove(handler)

    def trigger(self, alert: Alert) -> int:
        """
        Send alert to all registered handlers.

        Args:
            alert: Alert to send

        Returns:
            Number of handlers that successfully sent the alert
        """
        success_count = 0
        for handler in self._handlers:
            try:
                if handler.send(alert):
                    success_count += 1
            except Exception as e:
                logger.error(f"Handler {handler.__class__.__name__} failed: {e}")
        return success_count

    def trigger_info(self, sbc_name: str, message: str, details: str = None) -> int:
        """Convenience method to trigger an INFO alert."""
        alert = Alert(
            level=AlertLevel.INFO,
            sbc_name=sbc_name,
            message=message,
            details=details,
        )
        return self.trigger(alert)

    def trigger_warning(self, sbc_name: str, message: str, details: str = None) -> int:
        """Convenience method to trigger a WARNING alert."""
        alert = Alert(
            level=AlertLevel.WARNING,
            sbc_name=sbc_name,
            message=message,
            details=details,
        )
        return self.trigger(alert)

    def trigger_critical(self, sbc_name: str, message: str, details: str = None) -> int:
        """Convenience method to trigger a CRITICAL alert."""
        alert = Alert(
            level=AlertLevel.CRITICAL,
            sbc_name=sbc_name,
            message=message,
            details=details,
        )
        return self.trigger(alert)

    def close(self) -> None:
        """Close all handlers."""
        for handler in self._handlers:
            try:
                handler.close()
            except Exception as e:
                logger.error(f"Error closing handler: {e}")
        self._handlers.clear()
