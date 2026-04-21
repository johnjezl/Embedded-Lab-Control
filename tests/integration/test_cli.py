"""Integration tests for labctl CLI."""

import pytest
from click.testing import CliRunner

from labctl.core import audit
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


@pytest.fixture
def claim_runner(tmp_path, monkeypatch):
    """CLI runner with a throwaway DB so claim commands write nowhere real."""
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"database_path: {db_path}\n"
        "claims:\n"
        "  enabled: true\n"
        "  default_duration_minutes: 30\n"
        "  max_duration_minutes: 60\n"
        "  min_duration_minutes: 1\n"
        "  grace_period_seconds: 0\n"
    )

    # Every CLI invocation creates a fresh manager from config — we
    # seed the DB via a manager instance up front.
    from labctl.core.manager import get_manager

    manager = get_manager(db_path)
    manager.create_sbc(name="pi-5-1", project="test")
    manager.create_sbc(name="pi-5-2", project="test")

    runner = CliRunner()
    return runner, config_path, manager


class TestClaimCommands:
    """Integration tests for claim/release/renew/claims CLI commands."""

    def test_claim_and_release_round_trip(self, claim_runner):
        runner, config, manager = claim_runner

        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "10m", "-r", "bringup"],
        )
        assert result.exit_code == 0, result.output
        assert "Claimed 'pi-5-1'" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "list"])
        assert result.exit_code == 0
        assert "pi-5-1" in result.output

        result = runner.invoke(main, ["-c", str(config), "release", "pi-5-1"])
        assert result.exit_code == 0

        result = runner.invoke(main, ["-c", str(config), "claims", "list"])
        assert result.exit_code == 0
        assert "No active claims" in result.output

    def test_renew_show_history_and_stats(self, claim_runner):
        runner, config, manager = claim_runner

        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "10m", "-r", "bringup"],
        )
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            main,
            ["-c", str(config), "renew", "pi-5-1", "-d", "20m"],
        )
        assert result.exit_code == 0, result.output
        assert "Renewed claim on 'pi-5-1'" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "show", "pi-5-1"])
        assert result.exit_code == 0, result.output
        assert "held by" in result.output
        assert "reason: bringup" in result.output
        assert "renewed 1x" in result.output

        result = runner.invoke(main, ["-c", str(config), "release", "pi-5-1"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            main,
            ["-c", str(config), "claims", "history", "pi-5-1"],
        )
        assert result.exit_code == 0, result.output
        assert "Last 1 claim(s) on 'pi-5-1':" in result.output
        assert "[released]" in result.output
        assert "reason: bringup" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "stats"])
        assert result.exit_code == 0, result.output
        assert "Total claims:" in result.output
        assert "Released:" in result.output

    def test_claim_duration_out_of_bounds(self, claim_runner):
        runner, config, _ = claim_runner
        # Config max is 60 minutes; 4h should be rejected.
        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "4h", "-r", "too long"],
        )
        assert result.exit_code != 0
        assert "out of bounds" in result.output.lower()

    def test_claim_conflict_exit_nonzero(self, claim_runner):
        runner, config, manager = claim_runner
        # First claim via manager (different session id)
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-r", "mine"],
        )
        assert result.exit_code != 0
        assert "already claimed" in result.output

    def test_force_release_overrides(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            [
                "-c",
                str(config),
                "force-release",
                "pi-5-1",
                "-r",
                "operator takeover",
            ],
        )
        assert result.exit_code == 0
        assert manager.get_active_claim("pi-5-1") is None

    def test_request_release_records_request(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            [
                "-c",
                str(config),
                "request-release",
                "pi-5-1",
                "-r",
                "need the bench",
            ],
        )
        assert result.exit_code == 0
        claim = manager.get_active_claim("pi-5-1")
        assert len(claim.pending_requests) == 1

        result = runner.invoke(main, ["-c", str(config), "claims", "show", "pi-5-1"])
        assert result.exit_code == 0, result.output
        assert "request from" in result.output
        assert "need the bench" in result.output

    def test_status_surfaces_claim(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other-agent",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="bringup",
        )
        result = runner.invoke(main, ["-c", str(config), "status"])
        assert result.exit_code == 0
        assert "other-agent" in result.output
        assert "CLAIM" in result.output

    def test_remove_blocked_by_claim_without_force(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="holder",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="r",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "remove", "pi-5-1", "--yes"],
        )
        assert result.exit_code != 0
        assert "claimed" in result.output.lower()
        assert manager.get_sbc_by_name("pi-5-1") is not None

    def test_remove_force_succeeds_while_claimed(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-2",
            agent_name="holder",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="r",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "remove", "pi-5-2", "--yes", "--force"],
        )
        assert result.exit_code == 0
        assert manager.get_sbc_by_name("pi-5-2") is None

    def test_claims_expire_sweeps(self, claim_runner):
        """labctl claims expire releases past-deadline claims."""
        import time

        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="short",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=1,
            reason="short",
        )
        time.sleep(1.2)
        result = runner.invoke(
            main,
            ["-c", str(config), "claims", "expire"],
        )
        assert result.exit_code == 0
        assert "expired" in result.output.lower() or "Released" in result.output
        assert manager.get_active_claim("pi-5-1") is None


@pytest.fixture
def activity_runner(tmp_path):
    """CLI runner with a throwaway DB for activity-tail tests."""
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database_path: {db_path}\n")

    from labctl.core.manager import get_manager

    manager = get_manager(db_path)
    runner = CliRunner()
    return runner, config_path, manager


class TestActivityCommands:
    """Integration tests for activity-stream CLI commands."""

    def test_activity_tail_lists_events(self, activity_runner):
        runner, config, manager = activity_runner
        with audit.activity_context("cli:alice", "cli"):
            manager.create_sbc(name="activity-pi", project="test")

        result = runner.invoke(main, ["-c", str(config), "activity", "tail"])

        assert result.exit_code == 0
        assert "create" in result.output
        assert "activity-pi" in result.output
        assert "cli:alice" in result.output

    def test_activity_tail_filters_by_actor(self, activity_runner):
        runner, config, manager = activity_runner
        with audit.activity_context("cli:alice", "cli"):
            manager.create_sbc(name="alice-pi", project="test")
        with audit.activity_context("cli:bob", "cli"):
            manager.create_sbc(name="bob-pi", project="test")

        result = runner.invoke(
            main,
            ["-c", str(config), "activity", "tail", "--actor", "cli:alice"],
        )

        assert result.exit_code == 0
        assert "alice-pi" in result.output
        assert "bob-pi" not in result.output
