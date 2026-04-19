"""Unit tests for the services-status module."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from labctl import services
from labctl.services import (
    DEFAULT_UNITS,
    ServiceStatus,
    _format_duration,
    _parse_systemctl_show,
    _parse_systemd_timestamp,
    check_service,
)


class TestParsing:
    def test_parse_systemctl_show(self):
        out = (
            "ActiveState=active\n"
            "SubState=running\n"
            "NRestarts=2\n"
            "Result=success\n"
            "ExecMainStatus=0\n"
        )
        props = _parse_systemctl_show(out)
        assert props["ActiveState"] == "active"
        assert props["NRestarts"] == "2"
        assert props["Result"] == "success"

    def test_parse_systemd_timestamp_valid(self):
        dt = _parse_systemd_timestamp("Sun 2026-04-19 16:08:26 PDT")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 4 and dt.day == 19

    def test_parse_systemd_timestamp_empty(self):
        assert _parse_systemd_timestamp("") is None
        assert _parse_systemd_timestamp("n/a") is None
        assert _parse_systemd_timestamp("0") is None

    def test_format_duration(self):
        assert _format_duration(timedelta(seconds=5)) == "5s"
        assert _format_duration(timedelta(minutes=2, seconds=3)) == "2m3s"
        assert _format_duration(timedelta(hours=3, minutes=15)) == "3h15m"
        assert _format_duration(timedelta(days=2, hours=4)) == "2d4h"
        # Negative = sentinel
        assert _format_duration(timedelta(seconds=-1)) == "-"


class TestServiceStatus:
    def test_healthy_property(self):
        s = ServiceStatus(unit="x", active_state="active", sub_state="running")
        assert s.healthy

        s = ServiceStatus(unit="x", active_state="failed", sub_state="dead")
        assert not s.healthy

        s = ServiceStatus(unit="x", active_state="activating", sub_state="start")
        assert not s.healthy

    def test_uptime_str_no_since(self):
        s = ServiceStatus(unit="x")
        assert s.uptime_str() == "-"


class TestCheckService:
    def test_active_service(self):
        """Healthy service returns parsed status with no recent_errors fetch."""
        show_output = (
            "ActiveState=active\n"
            "SubState=running\n"
            "ActiveEnterTimestamp=Sun 2026-04-19 16:08:26 PDT\n"
            "NRestarts=0\n"
            "ExecMainStatus=0\n"
            "Result=success\n"
            "LoadState=loaded\n"
        )

        def fake_run(cmd, timeout=5.0):
            if "show" in cmd:
                return 0, show_output
            return 0, ""

        with patch.object(services, "_run", side_effect=fake_run), patch.object(
            services.shutil, "which", return_value="/bin/systemctl"
        ):
            status = check_service("fake.service")

        assert status.active_state == "active"
        assert status.sub_state == "running"
        assert status.n_restarts == 0
        assert status.healthy
        # We skip journal fetch for healthy services
        assert status.recent_errors == []

    def test_failed_service_fetches_recent_errors(self):
        show_output = (
            "ActiveState=failed\n"
            "SubState=failed\n"
            "ActiveEnterTimestamp=Sun 2026-04-19 16:08:26 PDT\n"
            "NRestarts=5\n"
            "ExecMainStatus=1\n"
            "Result=exit-code\n"
            "LoadState=loaded\n"
        )
        journal_output = (
            "2026-04-19T16:08:00 host u[1]: boom\n"
            "2026-04-19T16:08:05 host u[1]: boom\n"
            "2026-04-19T16:08:10 host u[1]: other error\n"
        )
        calls: list[list[str]] = []

        def fake_run(cmd, timeout=5.0):
            calls.append(cmd)
            if "show" in cmd:
                return 0, show_output
            if cmd[0] == "journalctl":
                return 0, journal_output
            return 0, ""

        with patch.object(services, "_run", side_effect=fake_run), patch.object(
            services.shutil, "which", return_value="/bin/whatever"
        ):
            status = check_service("fake.service")

        assert status.active_state == "failed"
        assert not status.healthy
        assert status.n_restarts == 5
        # Distinct messages preserved, duplicates collapsed.
        assert len(status.recent_errors) == 2
        # Confirm journalctl was indeed invoked.
        assert any(c[0] == "journalctl" for c in calls)

    def test_not_loaded_unit_reports_error(self):
        show_output = "ActiveState=inactive\nLoadState=not-found\n"

        def fake_run(cmd, timeout=5.0):
            return 0, show_output

        with patch.object(services, "_run", side_effect=fake_run), patch.object(
            services.shutil, "which", return_value="/bin/systemctl"
        ):
            status = check_service("nope.service")

        assert status.error is not None
        assert "not loaded" in status.error
        assert status.active_state == "not-found"

    def test_missing_systemctl(self):
        with patch.object(services.shutil, "which", return_value=None):
            status = check_service("anything")
        assert status.error == "systemctl not found"

    def test_default_units_cover_ser2net_and_labctl(self):
        assert "ser2net" in DEFAULT_UNITS
        assert "labctl-monitor" in DEFAULT_UNITS
        assert "labctl-mcp" in DEFAULT_UNITS
        assert "labctl-web" in DEFAULT_UNITS
