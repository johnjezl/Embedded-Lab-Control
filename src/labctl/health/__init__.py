"""
Health monitoring module for lab controller.

Provides health checks, status tracking, and alerting for SBCs.
"""

from labctl.health.alerts import (
    Alert,
    AlertHandler,
    AlertLevel,
    AlertManager,
    ConsoleAlertHandler,
    EmailAlertHandler,
    LogAlertHandler,
    SlackAlertHandler,
)
from labctl.health.checks import (
    CheckResult,
    CheckType,
    HealthChecker,
    HealthCheckSummary,
)
from labctl.health.daemon import MonitorDaemon, format_check_table

__all__ = [
    # Checks
    "CheckType",
    "CheckResult",
    "HealthChecker",
    "HealthCheckSummary",
    # Alerts
    "Alert",
    "AlertLevel",
    "AlertHandler",
    "AlertManager",
    "LogAlertHandler",
    "ConsoleAlertHandler",
    "EmailAlertHandler",
    "SlackAlertHandler",
    # Daemon
    "MonitorDaemon",
    "format_check_table",
]
