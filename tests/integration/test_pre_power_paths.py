"""Every path that powers a board on drives its pre_power bindings first
(issue #36), and refuses without switching power when a relay can't be
opened."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.actuators.base import DriverError, WriteOutcome
from labctl.actuators.mock import MockRelayDriver
from labctl.cli import main
from labctl.core.config import Config, Ser2NetConfig, SerialConfig
from labctl.core.manager import get_manager
from labctl.core.models import (
    ChannelState,
    DesiredState,
    DriverName,
    PlugType,
    PortType,
    SamplePhase,
    ShapeMode,
)

STRAP_CLOSED = (1, True, WriteOutcome.OK)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A board on a plug with an asserted recovery strap (active=closed)
    on a mock relay, and a power controller that records what the relay
    had been told when power came on."""
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database_path: {db_path}\n")

    manager = get_manager(db_path)
    sbc = manager.create_sbc(name="board", project="test")
    manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
    manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/lab/board", 4100)
    actuator = manager.create_actuator(
        "relay-a", DriverName.LCUS1_SERIAL, device_path="/dev/lab/relay-a"
    )
    channel = manager.add_actuator_channel(
        actuator.id, 1, default_state=ChannelState.OPEN
    )
    binding = manager.create_binding(
        sbc.id,
        "recovery_mode",
        channel.id,
        shape_mode=ShapeMode.LATCH,
        shape_active=ChannelState.CLOSED,
        sample_phase=SamplePhase.PRE_POWER,
    )
    manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

    driver = MockRelayDriver(channel_count=2)
    monkeypatch.setattr("labctl.actuators.runtime.get_driver", lambda *a, **kw: driver)
    monkeypatch.setattr("labctl.actuators.runtime.time.sleep", lambda s: None)

    events = []
    controller = MagicMock()
    controller.power_off.side_effect = lambda: events.append("off") or True
    controller.power_on.side_effect = (
        lambda: events.append(("on", list(driver.write_log))) or True
    )
    controller.get_state.return_value = MagicMock(value="on")
    monkeypatch.setattr(
        "labctl.power.base.PowerController.from_plug",
        lambda plug: controller,
    )

    runner = CliRunner()

    def run(*args):
        return runner.invoke(main, ["-c", str(config_path), *args])

    return type(
        "Lab",
        (),
        {
            "manager": manager,
            "db_path": db_path,
            "driver": driver,
            "controller": controller,
            "events": events,
            "run": staticmethod(run),
        },
    )


def _strap_closed_before_on(lab):
    ons = [e for e in lab.events if isinstance(e, tuple)]
    assert ons, f"power never came on: {lab.events}"
    assert all(STRAP_CLOSED in log for _, log in ons), lab.events


@pytest.fixture
def unopenable(lab, monkeypatch):
    def refuse(transport):
        raise DriverError("permission denied opening /dev/lab/relay-a: hint")

    monkeypatch.setattr(lab.driver, "open", refuse)
    return lab


# --- CLI ---


class TestCli:
    def test_power_on(self, lab):
        result = lab.run("power", "on", "board")
        assert result.exit_code == 0, result.output
        _strap_closed_before_on(lab)

    def test_power_cycle(self, lab):
        result = lab.run("power", "cycle", "board", "--yes")
        assert result.exit_code == 0, result.output
        assert lab.events[0] == "off"
        _strap_closed_before_on(lab)

    def test_power_cycle_nan_delay_is_one_error_line(self, lab):
        result = lab.run("power", "cycle", "board", "--delay", "nan", "--yes")
        assert result.exit_code == 1
        assert (
            "Error: delay must be a number of seconds from 0 to 86400" in result.output
        )
        assert "Traceback" not in result.output
        lab.controller.power_off.assert_not_called()

    def test_power_on_sets_straps_of_boards_sharing_the_plug(self, lab):
        """The outlet powers every board on it, so a board sharing it has
        its strap set too, before power comes on."""
        manager = lab.manager
        other = manager.create_sbc(name="board-b", project="test")
        manager.assign_power_plug(other.id, PlugType.TASMOTA, "10.0.0.5", 1)
        actuator = manager.get_actuator_by_name("relay-a")
        channel2 = manager.add_actuator_channel(
            actuator.id, 2, default_state=ChannelState.OPEN
        )
        b2 = manager.create_binding(
            other.id,
            "recovery_mode",
            channel2.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
            sample_phase=SamplePhase.PRE_POWER,
        )
        manager.update_binding_desired_state(b2.id, DesiredState.ASSERTED)

        result = lab.run("power", "on", "board")
        assert result.exit_code == 0, result.output
        ((_, log),) = [e for e in lab.events if isinstance(e, tuple)]
        assert STRAP_CLOSED in log
        assert (2, True, WriteOutcome.OK) in log

    def test_power_cycle_dry_run_refuses_a_delay_the_real_run_would(self, lab):
        result = lab.run(
            "power", "cycle", "board", "--delay", "nan", "--dry-run", "--yes"
        )
        assert result.exit_code == 1
        assert "Error: delay must be a number of seconds" in result.output
        assert "Dry run" not in result.output

    def test_power_all_on(self, lab):
        result = lab.run("power-all", "on", "--yes")
        assert result.exit_code == 0, result.output
        assert "board: ON OK" in result.output
        _strap_closed_before_on(lab)

    def test_power_all_on_unopenable_relay(self, unopenable):
        result = unopenable.run("power-all", "on", "--yes")
        assert "board: ERROR - " in result.output
        assert "permission denied" in result.output
        unopenable.controller.power_on.assert_not_called()

    @pytest.fixture
    def console(self):
        """The serial console shows the boot pattern straight away."""
        from labctl.serial.capture import CaptureResult

        with patch(
            "labctl.serial.boot_test.capture_serial_output",
            return_value=CaptureResult(output="login:", lines=1, pattern_matched=True),
        ):
            yield

    def test_boot_test(self, lab, console):
        result = lab.run("boot-test", "board", "-e", "login", "--no-deploy", "-r", "2")
        assert result.exit_code == 0, result.output
        assert "2/2" in result.output
        assert lab.events.count("off") == 2
        _strap_closed_before_on(lab)

    def test_boot_test_plug_on_failure_fails_the_run(self, lab, console):
        lab.controller.power_on.side_effect = lambda: False
        result = lab.run("boot-test", "board", "-e", "login", "--no-deploy", "-r", "1")
        assert "0/1" in result.output
        assert "power_on failed on 'board'" in result.output

    def test_boot_test_unopenable_relay_fails_each_run(self, unopenable, console):
        """Like a plug failure: each run is recorded as failed, with why."""
        result = unopenable.run(
            "boot-test", "board", "-e", "login", "--no-deploy", "-r", "2"
        )
        assert result.exit_code == 0, result.output
        assert "0/2" in result.output
        assert "actuator 'relay-a' for 'recovery_mode' can't be used" in (result.output)
        assert "Traceback" not in result.output
        unopenable.controller.power_off.assert_not_called()

    @pytest.fixture
    def sdwire(self, lab):
        device = lab.manager.create_sdwire_device("sdw-1", "bdgrd_sdwirec_001")
        lab.manager.assign_sdwire(lab.manager.get_sbc_by_name("board").id, device.id)
        ctrl = MagicMock()
        ctrl.get_block_device.return_value = "/dev/sdb"
        ctrl.flash_image.return_value = {
            "bytes_written": 3,
            "elapsed_seconds": 0.1,
            "block_device": "/dev/sdb",
        }
        ctrl.update_files.return_value = {
            "copied": ["kernel.img"],
            "renamed": [],
            "deleted": [],
        }
        with patch("labctl.sdwire.controller.SDWireController", return_value=ctrl):
            yield ctrl

    def test_sdwire_flash_reboot(self, lab, sdwire, tmp_path):
        image = tmp_path / "image.img"
        image.write_bytes(b"img")
        result = lab.run("sdwire", "flash", "board", str(image), "--yes")
        assert result.exit_code == 0, result.output
        _strap_closed_before_on(lab)

    def test_sdwire_flash_unopenable_relay_refused_up_front(
        self, unopenable, sdwire, tmp_path
    ):
        """Found before the board is switched off and the card written."""
        image = tmp_path / "image.img"
        image.write_bytes(b"img")
        result = unopenable.run("sdwire", "flash", "board", str(image), "--yes")
        assert result.exit_code == 1
        assert "Error: actuator 'relay-a' for 'recovery_mode' can't be used" in (
            result.output
        )
        assert "Nothing changed." in result.output
        unopenable.controller.power_off.assert_not_called()
        sdwire.switch_to_host.assert_not_called()
        sdwire.flash_image.assert_not_called()

    def test_sdwire_flash_no_reboot_skips_the_relay_check(
        self, unopenable, sdwire, tmp_path
    ):
        image = tmp_path / "image.img"
        image.write_bytes(b"img")
        result = unopenable.run(
            "sdwire", "flash", "board", str(image), "--no-reboot", "--yes"
        )
        assert result.exit_code == 0, result.output
        sdwire.flash_image.assert_called_once()

    def test_sdwire_flash_strap_failure_after_flash(self, lab, sdwire, tmp_path):
        """The relay passes the check but the write fails at reboot: the
        error says the board was flashed and left off."""
        image = tmp_path / "image.img"
        image.write_bytes(b"img")
        lab.driver.next_write_outcome = WriteOutcome.WRITE_FAILED
        result = lab.run("sdwire", "flash", "board", str(image), "--yes")
        assert result.exit_code == 1
        assert "Error: flashed, but the power cycle failed and board may be off: " in (
            result.output
        )
        assert "'board' was left powered off" in result.output
        lab.controller.power_on.assert_not_called()

    def test_sdwire_update_reboot(self, lab, sdwire, tmp_path):
        source = tmp_path / "kernel.img"
        source.write_bytes(b"img")
        result = lab.run(
            "sdwire",
            "update",
            "board",
            "-p",
            "1",
            "--copy",
            f"{source}:kernel.img",
            "--reboot",
            "--yes",
        )
        assert result.exit_code == 0, result.output
        _strap_closed_before_on(lab)

    def test_sdwire_update_unopenable_relay(self, unopenable, sdwire, tmp_path):
        source = tmp_path / "kernel.img"
        source.write_bytes(b"img")
        result = unopenable.run(
            "sdwire",
            "update",
            "board",
            "-p",
            "1",
            "--copy",
            f"{source}:kernel.img",
            "--reboot",
            "--yes",
        )
        assert result.exit_code == 1
        assert "Error: actuator 'relay-a' for 'recovery_mode' can't be used" in (
            result.output
        )
        unopenable.controller.power_off.assert_not_called()
        sdwire.update_files.assert_not_called()


# --- MCP ---


class TestMcp:
    @pytest.fixture(autouse=True)
    def _manager(self, lab):
        # The two-step confirmation (D012) is tested elsewhere.
        with (
            patch("labctl.mcp_server._get_manager", return_value=lab.manager),
            patch("labctl.mcp_server._confirmation_required", return_value=False),
        ):
            yield

    def test_power_on(self, lab):
        from labctl.mcp_server import power_on

        assert power_on(sbc_name="board").startswith("Power ON: board")
        _strap_closed_before_on(lab)

    def test_power_cycle(self, lab):
        from labctl.mcp_server import power_cycle

        assert power_cycle(sbc_name="board", delay=0.0).startswith("Power cycled")
        assert lab.events[0] == "off"
        _strap_closed_before_on(lab)

    def test_power_on_unopenable_relay(self, unopenable):
        from labctl.mcp_server import power_on

        result = power_on(sbc_name="board")
        assert result.startswith("Error: ")
        assert "permission denied" in result
        unopenable.controller.power_on.assert_not_called()

    def test_power_cycle_unopenable_relay_leaves_board_on(self, unopenable):
        from labctl.mcp_server import power_cycle

        result = power_cycle(sbc_name="board", delay=0.0)
        assert result.startswith(
            "Error: actuator 'relay-a' for 'recovery_mode' can't be used"
        )
        unopenable.controller.power_off.assert_not_called()


# --- Web ---


@pytest.fixture
def client(lab, monkeypatch):
    from labctl.web.app import create_app

    # The REST cycle rate limit is per board name, module-wide.
    monkeypatch.setattr("labctl.web.api._power_cycle_times", {})

    config = Config(
        database_path=lab.db_path,
        serial=SerialConfig(dev_dir=Path("/dev/lab")),
        ser2net=Ser2NetConfig(enabled=False),
    )
    app = create_app(config)
    app.config["TESTING"] = True
    return app.test_client()


class TestWeb:
    @pytest.mark.parametrize("action", ["on", "cycle"])
    def test_api(self, lab, client, action):
        resp = client.post(
            "/api/sbcs/board/power", json={"action": action, "delay": 0.0}
        )
        assert resp.status_code == 200, resp.get_json()
        _strap_closed_before_on(lab)

    @pytest.mark.parametrize("action", ["on", "cycle"])
    def test_api_unopenable_relay(self, unopenable, client, action):
        resp = client.post(
            "/api/sbcs/board/power", json={"action": action, "delay": 0.0}
        )
        # Refused before anything was switched.
        assert resp.status_code == 409
        assert resp.get_json()["switched"] is False
        assert "permission denied" in resp.get_json()["error"]
        unopenable.controller.power_on.assert_not_called()
        unopenable.controller.power_off.assert_not_called()

    @pytest.mark.parametrize(
        "delay", ["5", -1, None, True, "Infinity", 1e20, "10" * 200]
    )
    def test_api_bad_delay_refused_before_power_off(self, lab, client, delay):
        if delay == "Infinity":  # the JSON token, which Python parses to inf
            body = '{"action": "cycle", "delay": Infinity}'
        elif delay == "10" * 200:  # an int too large for a float
            body = '{"action": "cycle", "delay": ' + delay + "}"
        else:
            body = json.dumps({"action": "cycle", "delay": delay})
        resp = client.post(
            "/api/sbcs/board/power", data=body, content_type="application/json"
        )
        assert resp.status_code == 400
        assert "delay" in resp.get_json()["error"]
        lab.controller.power_off.assert_not_called()

    @pytest.mark.parametrize("action", ["on", "cycle"])
    def test_view(self, lab, client, action):
        resp = client.post(f"/sbc/board/power/{action}")
        assert resp.status_code == 302
        _strap_closed_before_on(lab)

    @pytest.mark.parametrize("action", ["on", "cycle"])
    def test_view_unopenable_relay(self, unopenable, client, action):
        resp = client.post(f"/sbc/board/power/{action}", follow_redirects=True)
        assert b"permission denied" in resp.data
        unopenable.controller.power_on.assert_not_called()
        unopenable.controller.power_off.assert_not_called()
