"""MCP tools that run long keep the caller's claim alive (issue #2)."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_mcp_server import (  # noqa: F401 (fixtures)
    _skip_destructive_confirmation,
    manager,
    populated_manager,
)


@pytest.fixture
def keepalive_env(populated_manager, tmp_path, monkeypatch):  # noqa: F811
    """Claims on, this test's directory allowed for host paths, and the
    keepalive thread beating every few milliseconds."""
    from labctl.core import claim_keepalive
    from labctl.core.config import ClaimsConfig, Config

    for name in ("local.bin", "test.img"):
        (tmp_path / name).write_bytes(b"\0")
    config = Config()
    config.claims = ClaimsConfig(
        enabled=True,
        default_duration_minutes=30,
        max_duration_minutes=60,
        min_duration_minutes=1,
        grace_period_seconds=60,
    )
    config.mcp.allowed_read_paths = [str(tmp_path)]
    config.database_path = tmp_path / "test.db"
    monkeypatch.setattr(claim_keepalive, "MIN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(claim_keepalive, "MAX_INTERVAL_SECONDS", 0.01)
    with (
        patch("labctl.mcp_server._get_manager", return_value=populated_manager),
        patch("labctl.mcp_server._get_config", return_value=config),
        patch("labctl.mcp_server._SESSION_ID", "test-session-1"),
        patch("labctl.mcp_server._SESSION_KIND", "mcp-stdio"),
        patch("labctl.mcp_server._AGENT_NAME", "test-agent"),
    ):
        yield populated_manager


def _claim_as_caller():
    from labctl.mcp_server import claim_sbc

    result = json.loads(claim_sbc(sbc_name="test-sbc-1", reason="r"))
    assert result["sbc_name"] == "test-sbc-1", result


def _claim_as_other(manager):  # noqa: F811
    manager.claim_sbc(
        sbc_name="test-sbc-1",
        agent_name="other-agent",
        session_id="other-session",
        session_kind="mcp-http",
        duration_seconds=600,
        reason="mine now",
    )


def _count_touches(manager):  # noqa: F811
    """Wrap the keepalive's heartbeat; returns (list of calls, event set
    on each)."""
    calls = []
    touched = threading.Event()
    real = manager.heartbeat_held_claim

    def heartbeat(sbc_name, actor, **kwargs):
        result = real(sbc_name, actor, **kwargs)
        calls.append(sbc_name)
        touched.set()
        return result

    manager.heartbeat_held_claim = heartbeat
    return calls, touched


def _keepalive_threads():
    return [t for t in threading.enumerate() if t.name.startswith("claim-keepalive")]


def _ok():
    from labctl.serial.capture import CaptureResult

    return CaptureResult(
        output="ok", lines=1, pattern_matched=True, elapsed_seconds=1.0
    )


class TestBootTest:
    def _boot_test(self, capture, runs=3):
        from labctl.mcp_server import boot_test

        cycle = MagicMock(return_value=True)
        with (
            patch("labctl.serial.boot_test.capture_serial_output", capture),
            patch("labctl.serial.boot_test.time.sleep"),
            patch("labctl.actuators.runtime.power_cycle_with_bindings", cycle),
            patch("labctl.power.base.PowerController.from_plug"),
        ):
            result = boot_test(sbc_name="test-sbc-1", expect_pattern="ok", runs=runs)
        return result, cycle

    def test_heartbeats_the_claim_on_every_run(self, keepalive_env):
        _claim_as_caller()
        calls, _ = _count_touches(keepalive_env)
        result, cycle = self._boot_test(MagicMock(return_value=_ok()))
        assert "3/3 boots successful" in result
        assert cycle.call_count == 3
        assert len(calls) >= 3  # at least one per run's checkpoint
        assert not _keepalive_threads()

    def test_heartbeats_during_a_long_run(self, keepalive_env):
        _claim_as_caller()
        _, touched = _count_touches(keepalive_env)

        def slow_capture(**kwargs):
            touched.clear()
            # The background thread beats while the capture runs.
            assert touched.wait(5)
            return _ok()

        result, _ = self._boot_test(slow_capture, runs=1)
        assert "1/1 boots successful" in result

    def test_stops_when_the_claim_is_lost(self, keepalive_env):
        _claim_as_caller()

        def capture(**kwargs):
            keepalive_env.force_release_claim("test-sbc-1", reason="needed it")
            return _ok()

        result, cycle = self._boot_test(capture)
        assert cycle.call_count == 1
        assert "STOPPED after 1 of 3 runs" in result
        assert "no longer held" in result
        assert result.startswith("Stopped: ")
        assert "Result: INCOMPLETE, 1/1 boots successful" in result

    def test_stopped_before_the_first_run(self, keepalive_env):
        """Someone claims the board while the test starts: no boots, and
        no normal "0/0 (0%)" result."""
        _claim_as_caller()

        from labctl.core.claim_keepalive import ClaimKeepalive

        real_start = ClaimKeepalive.start

        def start_then_lose(self):
            result = real_start(self)
            keepalive_env.force_release_claim("test-sbc-1", reason="r")
            return result

        with patch.object(ClaimKeepalive, "start", start_then_lose):
            result, cycle = self._boot_test(MagicMock(return_value=_ok()))
        cycle.assert_not_called()
        assert result.startswith("Stopped: ")
        assert "STOPPED after 0 of 3 runs" in result
        assert "Result: STOPPED before the first run" in result
        assert "0/0" not in result

    def test_database_errors_do_not_escape(self, keepalive_env, monkeypatch):
        """A database error in the per-run check is a missed beat: the
        test goes on."""
        import sqlite3

        _claim_as_caller()

        def down(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def capture(**kwargs):
            monkeypatch.setattr(keepalive_env, "check_claim", down)
            monkeypatch.setattr(keepalive_env, "heartbeat_held_claim", down)
            return _ok()

        result, cycle = self._boot_test(capture)
        assert cycle.call_count == 3
        assert "3/3 boots successful" in result

    def test_stops_when_someone_else_claims_it(self, keepalive_env):
        """No claim at the start (require_claim off): unchanged, until
        another agent claims the board mid-test."""
        captures = []

        def capture(**kwargs):
            captures.append(1)
            if len(captures) == 2:
                _claim_as_other(keepalive_env)
            return _ok()

        result, cycle = self._boot_test(capture)
        assert cycle.call_count == 2
        assert "STOPPED after 2 of 3 runs" in result
        assert "now claimed by 'other-agent'" in result

    def test_without_claims_unchanged(self, keepalive_env):
        result, cycle = self._boot_test(MagicMock(return_value=_ok()))
        assert cycle.call_count == 3
        assert "STOPPED" not in result
        assert keepalive_env.get_open_claim("test-sbc-1") is None

    def test_claimed_by_someone_else_is_refused_without_heartbeat(self, keepalive_env):
        _claim_as_other(keepalive_env)
        before = keepalive_env.get_open_claim("test-sbc-1").expires_at
        result, cycle = self._boot_test(MagicMock(return_value=_ok()))
        assert json.loads(result)["error"] == "sbc_claimed"
        cycle.assert_not_called()
        assert keepalive_env.get_open_claim("test-sbc-1").expires_at == before


class TestSerialCapture:
    def test_heartbeats_while_capturing(self, keepalive_env):
        from labctl.mcp_server import serial_capture

        _claim_as_caller()
        _, touched = _count_touches(keepalive_env)

        def capture(**kwargs):
            touched.clear()
            assert touched.wait(5)
            return _ok()

        with patch("labctl.serial.capture.capture_serial_output", capture):
            result = serial_capture(port_name="test-sbc-1", timeout=600)
        assert "ok" in result
        assert not _keepalive_threads()

    def test_without_a_claim_starts_no_thread(self, keepalive_env):
        from labctl.mcp_server import serial_capture

        seen = []

        def capture(**kwargs):
            seen.append(_keepalive_threads())
            return _ok()

        with patch("labctl.serial.capture.capture_serial_output", capture):
            serial_capture(port_name="test-sbc-1", timeout=600)
        assert seen == [[]]
        assert keepalive_env.get_open_claim("test-sbc-1") is None


class TestCardWrites:
    def test_sdwire_update_skips_the_reboot_once_the_claim_is_lost(
        self, keepalive_env, tmp_path
    ):
        from labctl.mcp_server import sdwire_update

        _claim_as_caller()
        ctrl = MagicMock()

        def update_files(*args, **kwargs):
            keepalive_env.force_release_claim("test-sbc-1", reason="r")
            return {"copied": ["kernel.img"], "renamed": [], "deleted": []}

        ctrl.update_files.side_effect = update_files
        power = MagicMock()
        power.power_off.return_value = True
        with (
            patch("labctl.sdwire.SDWireController", return_value=ctrl),
            patch("labctl.power.base.PowerController.from_plug", return_value=power),
            patch("time.sleep"),
        ):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=[f"{tmp_path}/local.bin:kernel.img"],
                reboot=True,
            )
        assert "Copied: kernel.img" in result
        assert "Not power cycled" in result and "left off" in result
        ctrl.switch_to_dut.assert_called_once()
        power.power_on.assert_not_called()
        assert not _keepalive_threads()

    def test_sdwire_update_left_off_names_a_new_claim_on_a_sharer(
        self, keepalive_env, tmp_path
    ):
        """No claim at the start; a board sharing the plug is claimed while
        the card is written: the text says that, not "your claim"."""
        from labctl.core.models import PlugType
        from labctl.mcp_server import sdwire_update

        other = keepalive_env.create_sbc(name="sharer")
        keepalive_env.assign_power_plug(other.id, PlugType.TASMOTA, "192.168.1.200", 1)
        ctrl = MagicMock()

        def update_files(*args, **kwargs):
            keepalive_env.claim_sbc(
                sbc_name="sharer",
                agent_name="other-agent",
                session_id="other-session",
                session_kind="mcp-http",
                duration_seconds=600,
                reason="r",
            )
            return {"copied": ["kernel.img"], "renamed": [], "deleted": []}

        ctrl.update_files.side_effect = update_files
        power = MagicMock()
        power.power_off.return_value = True
        with (
            patch("labctl.sdwire.SDWireController", return_value=ctrl),
            patch("labctl.power.base.PowerController.from_plug", return_value=power),
            patch("time.sleep"),
        ):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=[f"{tmp_path}/local.bin:kernel.img"],
                reboot=True,
            )
        assert (
            "test-sbc-1 is left off (not power cycled) because 'sharer' is now "
            "claimed by 'other-agent'"
        ) in result
        power.power_on.assert_not_called()

    def test_sdwire_update_reboots_while_the_claim_is_held(
        self, keepalive_env, tmp_path
    ):
        from labctl.mcp_server import sdwire_update

        _claim_as_caller()
        ctrl = MagicMock()
        ctrl.update_files.return_value = {
            "copied": ["kernel.img"],
            "renamed": [],
            "deleted": [],
        }
        power = MagicMock()
        power.power_off.return_value = True
        power.power_on.return_value = True
        with (
            patch("labctl.sdwire.SDWireController", return_value=ctrl),
            patch("labctl.power.base.PowerController.from_plug", return_value=power),
            patch("time.sleep"),
        ):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=[f"{tmp_path}/local.bin:kernel.img"],
                reboot=True,
            )
        assert "Power cycled" in result
        power.power_on.assert_called_once()

    def test_flash_image_skips_power_on_once_the_claim_is_lost(
        self, keepalive_env, tmp_path
    ):
        from labctl.mcp_server import flash_image

        _claim_as_caller()
        ctrl = MagicMock()
        ctrl.get_block_device.return_value = "/dev/sdb"

        def flash(*args, **kwargs):
            keepalive_env.force_release_claim("test-sbc-1", reason="r")
            return {
                "bytes_written": 1,
                "elapsed_seconds": 1.0,
                "block_device": "/dev/sdb",
            }

        ctrl.flash_image.side_effect = flash
        power = MagicMock()
        power.power_off.return_value = True
        with (
            patch("labctl.sdwire.SDWireController", return_value=ctrl),
            patch("labctl.power.base.PowerController.from_plug", return_value=power),
            patch("time.sleep"),
        ):
            result = flash_image(
                sbc_name="test-sbc-1",
                image_path=f"{tmp_path}/test.img",
                reboot=True,
            )
        assert "Flashed" in result
        assert "Not powered on" in result
        power.power_on.assert_not_called()
        assert not _keepalive_threads()

    def test_refused_call_starts_no_keepalive(self, keepalive_env):
        from labctl.mcp_server import sdwire_update

        _claim_as_caller()
        with patch("labctl.core.claim_keepalive.ClaimKeepalive.start") as start:
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=["/not/allowed/x.bin:kernel.img"],
            )
        assert result.startswith("Error")
        start.assert_not_called()
