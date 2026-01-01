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
