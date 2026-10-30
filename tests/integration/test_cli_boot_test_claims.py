"""CLI boot-test keeps the user's own claim alive and stops between runs
once it is lost or someone else claims the board (issue #2)."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.cli import _cli_session_id, main
from labctl.core.manager import ResourceManager
from labctl.core.models import PlugType, PortType


@pytest.fixture
def lab(tmp_path):
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"database_path: {db_path}\n"
        "claims:\n"
        "  enabled: true\n"
        "  grace_period_seconds: 0\n"
    )
    from labctl.core.manager import get_manager

    manager = get_manager(db_path)
    sbc = manager.create_sbc(name="pi-c", project="test")
    manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.6", 1)
    manager.assign_serial_port(
        sbc_id=sbc.id,
        port_type=PortType.CONSOLE,
        device_path="/dev/lab/pi-c",
        tcp_port=4100,
    )
    runner = CliRunner()

    def run(*args):
        return runner.invoke(main, ["-c", str(config_path), *args])

    return run, manager


def _claim(manager, session_id, agent):
    manager.claim_sbc(
        sbc_name="pi-c",
        agent_name=agent,
        session_id=session_id,
        session_kind="cli" if session_id.startswith("cli-") else "mcp-http",
        duration_seconds=600,
        reason="r",
    )


@pytest.fixture
def hardware():
    """No real plug or console: cycles and captures are mocked."""
    from labctl.serial.capture import CaptureResult

    cycle = MagicMock(return_value=True)
    capture = MagicMock(
        return_value=CaptureResult(
            output="login", lines=1, pattern_matched=True, elapsed_seconds=1.0
        )
    )
    with (
        patch("labctl.actuators.runtime.power_cycle_with_bindings", cycle),
        patch("labctl.power.PowerController.from_plug"),
        patch("labctl.serial.boot_test.capture_serial_output", capture),
        patch("labctl.serial.boot_test.time.sleep"),
    ):
        yield cycle, capture


ARGS = ("boot-test", "pi-c", "-e", "login", "--no-deploy", "-r", "3", "--yes")


def test_heartbeats_the_users_own_claim(lab, hardware):
    run, manager = lab
    cycle, _ = hardware
    _claim(manager, _cli_session_id(), "me")
    touched = []
    real = ResourceManager.touch_claim

    def touch(self, claim):
        touched.append(claim.session_id)
        return real(self, claim)

    with patch.object(ResourceManager, "touch_claim", touch):
        result = run(*ARGS)
    assert result.exit_code == 0, result.output
    assert "3/3" in result.output
    assert cycle.call_count == 3
    assert touched.count(_cli_session_id()) >= 3


def test_stops_when_the_users_claim_is_lost(lab, hardware):
    run, manager = lab
    cycle, capture = hardware
    _claim(manager, _cli_session_id(), "me")

    def lose(**kwargs):
        manager.force_release_claim("pi-c", reason="needed it")
        return capture.return_value

    capture.side_effect = lose
    result = run(*ARGS)
    assert result.exit_code == 1, result.output
    assert cycle.call_count == 1
    assert "STOPPED after 1 of 3 runs" in result.output
    assert "no longer held" in result.output


def test_stops_when_someone_else_claims_it_mid_test(lab, hardware):
    run, manager = lab
    cycle, capture = hardware

    def other_claims(**kwargs):
        _claim(manager, "mcp-http:1:agent-x", "agent-x")
        return capture.return_value

    capture.side_effect = other_claims
    result = run(*ARGS)
    assert result.exit_code == 1, result.output
    assert cycle.call_count == 1
    assert "now claimed by 'agent-x'" in result.output


def test_override_claim_runs_to_the_end(lab, hardware):
    """--override-claim started it over someone else's claim: that claim
    doesn't stop it between runs."""
    run, manager = lab
    cycle, _ = hardware
    _claim(manager, "mcp-http:1:agent-x", "agent-x")
    result = run(*ARGS, "--override-claim")
    assert result.exit_code == 0, result.output
    assert cycle.call_count == 3
    assert "STOPPED" not in result.output


def test_without_a_claim_unchanged(lab, hardware):
    run, manager = lab
    cycle, _ = hardware
    result = run(*ARGS)
    assert result.exit_code == 0, result.output
    assert cycle.call_count == 3
    assert manager.get_open_claim("pi-c") is None
