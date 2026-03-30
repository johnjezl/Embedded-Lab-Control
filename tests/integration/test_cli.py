"""Integration tests for labctl CLI."""

import pytest
from click.testing import CliRunner

from labctl.cli import main


@pytest.fixture
def runner():
    """Create a CLI test runner."""
    return CliRunner()


class TestMainCommand:
    """Tests for the main labctl command."""

    def test_version(self, runner):
        """Test --version flag."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "labctl" in result.output
        assert "0.1.0" in result.output

    def test_help(self, runner):
        """Test --help flag."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Lab Controller" in result.output
        assert "ports" in result.output
        assert "connect" in result.output


class TestDelayOption:
    """Tests for the global --delay option."""

    def test_help_shows_delay(self, runner):
        """Test that --delay appears in help output."""
        result = runner.invoke(main, ["--help"])
        assert "--delay" in result.output
        assert "-d" in result.output

    def test_delay_zero_no_message(self, runner):
        """Test that --delay 0 produces no waiting message."""
        result = runner.invoke(main, ["--delay", "0", "ports"])
        assert result.exit_code == 0
        assert "Waiting" not in result.output

    def test_delay_shows_message(self, runner):
        """Test that --delay shows waiting message."""
        result = runner.invoke(main, ["--delay", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting 0.01s" in result.output

    def test_delay_short_flag(self, runner):
        """Test -d short flag works."""
        result = runner.invoke(main, ["-d", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting 0.01s" in result.output

    def test_delay_quiet_suppresses_message(self, runner):
        """Test that --quiet suppresses the delay message."""
        result = runner.invoke(main, ["-q", "--delay", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting" not in result.output

    def test_delay_actually_delays(self, runner):
        """Test that delay actually waits before executing."""
        import time

        start = time.monotonic()
        result = runner.invoke(main, ["--delay", "0.2", "ports"])
        elapsed = time.monotonic() - start

        assert result.exit_code == 0
        assert elapsed >= 0.2

    def test_delay_with_version(self, runner):
        """Test that --delay works alongside --version."""
        result = runner.invoke(main, ["--delay", "0.01", "--version"])
        assert result.exit_code == 0
        assert "labctl" in result.output

    def test_delay_invalid_value(self, runner):
        """Test that invalid delay value shows error."""
        result = runner.invoke(main, ["--delay", "abc", "ports"])
        assert result.exit_code != 0


class TestPortsCommand:
    """Tests for the ports command."""

    def test_ports_runs(self, runner):
        """Test that ports command runs without error."""
        result = runner.invoke(main, ["ports"])
        # Should succeed even if no ports configured
        assert result.exit_code == 0

    def test_ports_verbose(self, runner):
        """Test ports command with verbose flag."""
        result = runner.invoke(main, ["-v", "ports"])
        assert result.exit_code == 0

    def test_ports_help(self, runner):
        """Test ports command help."""
        result = runner.invoke(main, ["ports", "--help"])
        assert result.exit_code == 0
        assert "List available serial ports" in result.output


class TestConnectCommand:
    """Tests for the connect command."""

    def test_connect_help(self, runner):
        """Test connect command help."""
        result = runner.invoke(main, ["connect", "--help"])
        assert result.exit_code == 0
        assert "Connect to a serial port console" in result.output

    def test_connect_missing_port(self, runner):
        """Test connect with missing port argument."""
        result = runner.invoke(main, ["connect"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output

    def test_connect_nonexistent_port(self, runner):
        """Test connect to non-existent port."""
        result = runner.invoke(main, ["connect", "nonexistent-port"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
