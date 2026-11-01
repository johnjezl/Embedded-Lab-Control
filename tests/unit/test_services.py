"""Unit tests for the services-status module."""

import os
import time
from datetime import datetime, timedelta, timezone
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


@pytest.fixture
def pacific_tz():
    """Run the test with the process timezone set to US Pacific.

    Only UTC/GMT and the process's own zone abbreviations can be resolved,
    so a "PDT" sample parses only when the process is on Pacific time,
    which CI runners (UTC) are not.
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


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

    def test_parse_systemd_timestamp_local_zone(self, pacific_tz):
        dt = _parse_systemd_timestamp("Sun 2026-04-19 16:08:26 PDT")
        assert dt == datetime(2026, 4, 19, 23, 8, 26, tzinfo=timezone.utc)

    def test_parse_systemd_timestamp_utc_on_another_zone(self, pacific_tz):
        """UTC is read as UTC, not as local time (#51)."""
        dt = _parse_systemd_timestamp("Sun 2026-04-19 23:08:26 UTC")
        assert dt == datetime(2026, 4, 19, 23, 8, 26, tzinfo=timezone.utc)

    def test_parse_systemd_timestamp_utc_any_host(self):
        """A UTC timestamp parses regardless of process TZ."""
        dt = _parse_systemd_timestamp("Sun 2026-04-19 23:08:26 UTC")
        assert dt == datetime(2026, 4, 19, 23, 8, 26, tzinfo=timezone.utc)

    def test_parse_systemd_timestamp_no_zone_is_local(self, pacific_tz):
        dt = _parse_systemd_timestamp("2026-04-19 16:08:26")
        assert dt == datetime(2026, 4, 19, 23, 8, 26, tzinfo=timezone.utc)

    def test_parse_systemd_timestamp_fall_back_hour(self, pacific_tz):
        """01:30 happens twice on 2026-11-01; the abbreviation says which."""
        pdt = _parse_systemd_timestamp("Sun 2026-11-01 01:30:00 PDT")
        pst = _parse_systemd_timestamp("Sun 2026-11-01 01:30:00 PST")
        assert pdt == datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)
        assert pst == datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)

    def test_parse_systemd_timestamp_unresolvable_zone(self, pacific_tz):
        """Another zone's abbreviation is ambiguous ("CST"), so no guess."""
        assert _parse_systemd_timestamp("Sun 2026-04-19 18:08:26 CST") is None

    def test_parse_systemd_timestamp_garbage(self):
        assert _parse_systemd_timestamp("yesterday") is None
        assert _parse_systemd_timestamp("Sun 2026-04-19") is None

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
            "ActiveEnterTimestamp=Sun 2026-04-19 23:08:26 UTC\n"
            "NRestarts=0\n"
            "ExecMainStatus=0\n"
            "Result=success\n"
            "LoadState=loaded\n"
        )
        show_envs = []

        def fake_run(cmd, timeout=5.0, env=None):
            if "show" in cmd:
                show_envs.append(env)
                return 0, show_output
            return 0, ""

        with (
            patch.object(services, "_run", side_effect=fake_run),
            patch.object(services.shutil, "which", return_value="/bin/systemctl"),
        ):
            status = check_service("fake.service")

        assert status.active_state == "active"
        assert status.sub_state == "running"
        assert status.n_restarts == 0
        assert status.healthy
        # systemctl is asked for UTC timestamps, which always parse (#51)
        assert show_envs[0]["TZ"] == "UTC"
        assert status.active_since == datetime(
            2026, 4, 19, 23, 8, 26, tzinfo=timezone.utc
        )
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

        def fake_run(cmd, timeout=5.0, env=None):
            calls.append(cmd)
            if "show" in cmd:
                return 0, show_output
            if cmd[0] == "journalctl":
                return 0, journal_output
            return 0, ""

        with (
            patch.object(services, "_run", side_effect=fake_run),
            patch.object(services.shutil, "which", return_value="/bin/whatever"),
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

        def fake_run(cmd, timeout=5.0, env=None):
            return 0, show_output

        with (
            patch.object(services, "_run", side_effect=fake_run),
            patch.object(services.shutil, "which", return_value="/bin/systemctl"),
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
