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
# The board's plug outlet, as the REST/web cycle guard keys it.
OUTLET = ("tasmota", "10.0.0.5", 1)


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

    def test_power_cycle_bad_stored_delay_says_how_to_clear_it(self, lab):
        """A delay stored before `edit --cycle-delay` checked the range."""
        import sqlite3

        with sqlite3.connect(lab.db_path) as db:
            db.execute("UPDATE sbcs SET power_cycle_delay_seconds = 1e9")
        result = lab.run("power", "cycle", "board", "-y")
        assert result.exit_code == 1
        assert "labctl edit board --cycle-delay -1" in result.output
        lab.controller.power_off.assert_not_called()

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

    def test_boot_test_unopenable_relay_refused_up_front(self, unopenable, console):
        """Found before the image is deployed or any run starts."""
        with patch("labctl.serial.boot_test.run_boot_test") as run_boot_test:
            result = unopenable.run(
                "boot-test", "board", "-e", "login", "--no-deploy", "-r", "2"
            )
        assert result.exit_code == 1
        assert "Error: actuator 'relay-a' for 'recovery_mode' can't be used" in (
            result.output
        )
        assert "Nothing changed." in result.output
        run_boot_test.assert_not_called()
        unopenable.controller.power_off.assert_not_called()

    def test_boot_test_relay_failing_mid_test_fails_the_run(
        self, lab, console, monkeypatch
    ):
        """A relay that goes away after the start is recorded as a failed
        run, like a plug failure."""
        from labctl.actuators import runtime

        real_check = runtime.check_power_on
        calls = []

        def check_then_fail(manager, sbc):
            calls.append(sbc.name)
            if len(calls) > 1:
                raise runtime.PrePowerCheckError("relay gone")
            return real_check(manager, sbc)

        monkeypatch.setattr(runtime, "check_power_on", check_then_fail)
        result = lab.run("boot-test", "board", "-e", "login", "--no-deploy", "-r", "1")
        assert result.exit_code == 0, result.output
        assert "0/1" in result.output
        assert "relay gone" in result.output

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

    def test_boot_test_deploy_powers_off_before_switching_the_card(
        self, lab, sdwire, console, tmp_path
    ):
        """The card never leaves a running board (bus contention)."""
        image = tmp_path / "kernel.img"
        image.write_bytes(b"img")
        sdwire.switch_to_host.side_effect = lambda: lab.events.append("host")
        result = lab.run(
            "boot-test", "board", "-e", "login", "-r", "1",
            "--image", str(image), "--dest", "kernel.img",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        assert lab.events[:2] == ["off", "host"], lab.events
        sdwire.update_files.assert_called_once()
        _strap_closed_before_on(lab)

    @pytest.mark.parametrize("failure", ["false", "raises"])
    def test_boot_test_deploy_refused_when_power_off_fails(
        self, lab, sdwire, console, tmp_path, failure
    ):
        image = tmp_path / "kernel.img"
        image.write_bytes(b"img")
        if failure == "false":
            lab.controller.power_off.side_effect = lambda: False
        else:
            lab.controller.power_off.side_effect = OSError("plug unreachable")
        result = lab.run(
            "boot-test", "board", "-e", "login", "-r", "1",
            "--image", str(image), "--dest", "kernel.img",
        )  # fmt: skip
        assert result.exit_code == 1
        assert "could not power off 'board' before switching its SD card" in (
            result.output
        )
        assert "nothing was written to the card" in result.output
        if failure == "raises":
            assert "plug unreachable" in result.output
        sdwire.switch_to_host.assert_not_called()
        sdwire.update_files.assert_not_called()
        lab.controller.power_on.assert_not_called()

    @pytest.mark.parametrize("failure", [RuntimeError, OSError])
    def test_boot_test_deploy_failure_after_power_off_says_board_left_off(
        self, lab, sdwire, console, tmp_path, failure
    ):
        """Any failure once the board is off is one Error line (no
        traceback), hands the card back, and says the board is off."""
        image = tmp_path / "kernel.img"
        image.write_bytes(b"img")
        sdwire.update_files.side_effect = failure("mount failed")
        result = lab.run(
            "boot-test", "board", "-e", "login", "-r", "1",
            "--image", str(image), "--dest", "kernel.img",
        )  # fmt: skip
        assert result.exit_code == 1
        assert not isinstance(result.exception, failure), result.exception
        assert "deploy to 'board' failed: mount failed" in result.output
        assert "left powered off" in result.output
        sdwire.switch_to_dut.assert_called_once()
        lab.controller.power_on.assert_not_called()

    @pytest.mark.parametrize("command", ["flash", "update"])
    def test_sdwire_write_refused_when_power_off_fails(
        self, lab, sdwire, tmp_path, command
    ):
        """The card is never switched away from a board that may be running
        (it used to carry on with a warning)."""
        image = tmp_path / "image.img"
        image.write_bytes(b"img")
        lab.controller.power_off.side_effect = lambda: False
        if command == "flash":
            args = ("sdwire", "flash", "board", str(image), "--no-reboot", "-y")
        else:
            args = (
                "sdwire", "update", "board", "-p", "1", "--copy",
                f"{image}:kernel.img", "-y",
            )  # fmt: skip
        result = lab.run(*args)
        assert result.exit_code == 1
        assert "could not power off 'board'" in result.output
        assert "nothing was written to the card" in result.output
        sdwire.switch_to_host.assert_not_called()
        sdwire.flash_image.assert_not_called()
        sdwire.update_files.assert_not_called()

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
    monkeypatch.setattr("labctl.web.api._power_cycles_in_progress", set())

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

    def test_api_on_strap_write_failure_is_not_switched(self, lab, client):
        lab.driver.next_write_outcome = WriteOutcome.WRITE_FAILED
        resp = client.post("/api/sbcs/board/power", json={"action": "on"})
        assert resp.status_code == 409
        assert resp.get_json()["switched"] is False
        lab.controller.power_on.assert_not_called()

    def test_api_cycle_strap_write_failure_after_power_off(self, lab, client):
        lab.driver.next_write_outcome = WriteOutcome.WRITE_FAILED
        resp = client.post(
            "/api/sbcs/board/power", json={"action": "cycle", "delay": 0.0}
        )
        assert resp.status_code == 500
        assert "left powered off" in resp.get_json()["error"]
        assert "switched" not in resp.get_json()

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


# --- The stored cycle delay on every path (#41) ---


class TestStoredCycleDelay:
    """The board's stored cycle delay is the default and the minimum of
    every power cycle, and an out-of-range one (stored before `edit
    --cycle-delay` checked the range) is refused before anything is
    switched or written."""

    @pytest.fixture
    def slept(self, lab, monkeypatch):
        waits = []
        monkeypatch.setattr("labctl.actuators.runtime.time.sleep", waits.append)
        return waits

    @staticmethod
    def _store(lab, value):
        import sqlite3

        with sqlite3.connect(lab.db_path) as db:
            db.execute("UPDATE sbcs SET power_cycle_delay_seconds = ?", (value,))

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
        with (
            patch("labctl.sdwire.controller.SDWireController", return_value=ctrl),
            patch("time.sleep"),
        ):
            yield ctrl

    @pytest.fixture
    def console(self):
        from labctl.serial.capture import CaptureResult

        with patch(
            "labctl.serial.boot_test.capture_serial_output",
            return_value=CaptureResult(output="login:", lines=1, pattern_matched=True),
        ):
            yield

    @pytest.fixture
    def image(self, tmp_path):
        path = tmp_path / "image.img"
        path.write_bytes(b"img")
        return path

    def test_sdwire_flash_reboot_waits_stored_delay(self, lab, sdwire, image, slept):
        self._store(lab, 8.0)
        result = lab.run("sdwire", "flash", "board", str(image), "--yes")
        assert result.exit_code == 0, result.output
        # time.sleep is patched process-wide; the others are settle waits.
        assert 8.0 in slept and 3.0 not in slept

    def test_sdwire_flash_dry_run_shows_delay(self, lab, sdwire, image):
        self._store(lab, 8.0)
        result = lab.run("sdwire", "flash", "board", str(image), "--dry-run")
        assert result.exit_code == 0, result.output
        assert (
            "power-cycle board (waiting 8.0s between off and on: the SBC minimum)"
            in result.output
        )

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_sdwire_flash_refuses_bad_stored_delay(self, lab, sdwire, image, dry_run):
        self._store(lab, 1e9)
        args = ["sdwire", "flash", "board", str(image), "--yes"]
        result = lab.run(*args, *(["--dry-run"] if dry_run else []))
        assert result.exit_code == 1
        assert "the cycle delay stored for 'board'" in result.output
        assert "labctl edit board --cycle-delay -1. Nothing changed." in result.output
        lab.controller.power_off.assert_not_called()
        sdwire.switch_to_host.assert_not_called()
        sdwire.flash_image.assert_not_called()

    def test_sdwire_flash_no_reboot_ignores_bad_stored_delay(self, lab, sdwire, image):
        """No power cycle, so no delay to check."""
        self._store(lab, 1e9)
        result = lab.run("sdwire", "flash", "board", str(image), "--no-reboot", "-y")
        assert result.exit_code == 0, result.output

    def test_sdwire_update_reboot_waits_stored_delay(self, lab, sdwire, slept):
        self._store(lab, 6.0)
        result = lab.run(
            "sdwire", "update", "board", "-p", "1", "-d", "x", "--reboot", "--yes"
        )
        assert result.exit_code == 0, result.output
        assert 6.0 in slept and 3.0 not in slept

    def test_sdwire_update_dry_run_shows_delay(self, lab, sdwire):
        result = lab.run(
            "sdwire", "update", "board", "-p", "1", "-d", "x", "--reboot", "--dry-run"
        )
        assert result.exit_code == 0, result.output
        assert (
            "power-cycle board (waiting 3.0s between off and on: the default minimum)"
            in result.output
        )

    def test_sdwire_update_refuses_bad_stored_delay(self, lab, sdwire):
        self._store(lab, -5.0)
        result = lab.run(
            "sdwire", "update", "board", "-p", "1", "-d", "x", "--reboot", "--yes"
        )
        assert result.exit_code == 1
        assert "the cycle delay stored for 'board'" in result.output
        lab.controller.power_off.assert_not_called()
        sdwire.update_files.assert_not_called()

    def test_boot_test_waits_stored_delay(self, lab, console, slept):
        self._store(lab, 4.5)
        result = lab.run("boot-test", "board", "-e", "login", "--no-deploy", "-r", "2")
        assert result.exit_code == 0, result.output
        assert slept.count(4.5) == 2 and 3.0 not in slept

    def test_boot_test_refuses_bad_stored_delay(self, lab, sdwire, console, image):
        self._store(lab, 1e9)
        result = lab.run(
            "boot-test", "board", "-i", str(image), "-d", "k.img", "-e", "login"
        )
        assert result.exit_code == 1
        assert "the cycle delay stored for 'board'" in result.output
        assert "Nothing changed." in result.output
        lab.controller.power_off.assert_not_called()
        sdwire.update_files.assert_not_called()

    @pytest.mark.parametrize("command", ["enter-recovery", "exit-recovery"])
    def test_recovery_refuses_bad_stored_delay(self, lab, slept, command):
        self._store(lab, 1e9)
        result = lab.run(command, "board", "--yes")
        assert result.exit_code == 1
        assert "labctl edit board --cycle-delay -1" in result.output
        assert "Traceback" not in result.output
        lab.controller.power_off.assert_not_called()
        assert slept == []

    def test_power_cycle_bad_stored_delay_is_one_error_line(self, lab):
        self._store(lab, 1e9)
        result = lab.run("power", "cycle", "board", "--delay", "5", "-y")
        assert result.exit_code == 1
        errors = [line for line in result.output.splitlines() if "Error" in line]
        assert errors == [
            "Error: the cycle delay stored for 'board' (1000000000.0) is out of "
            "range (0 to 86400 seconds); clear it with: labctl edit board "
            "--cycle-delay -1"
        ]
        lab.controller.power_off.assert_not_called()

    def test_mcp_power_cycle_waits_stored_minimum(self, lab, slept):
        from labctl import mcp_server

        self._store(lab, 5.0)
        with (
            patch.object(mcp_server, "_get_manager", return_value=lab.manager),
            patch.object(mcp_server, "_confirmation_required", return_value=False),
            patch.object(mcp_server, "_check_claim", return_value=None),
        ):
            result = mcp_server.power_cycle(sbc_name="board", delay=0.5)
        assert result.startswith("Power cycled: board (delay: 5.0s)")
        assert slept == [5.0]

    def test_api_cycle_defaults_to_stored_delay(self, lab, client, slept):
        self._store(lab, 2.5)
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["delay_seconds"] == 2.5
        assert slept == [2.5]

    def test_api_cycle_raises_delay_to_stored_minimum(self, lab, client, slept):
        self._store(lab, 2.5)
        resp = client.post(
            "/api/sbcs/board/power", json={"action": "cycle", "delay": 1}
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["delay_seconds"] == 2.5
        assert slept == [2.5]

    def test_api_cycle_refuses_bad_stored_delay(self, lab, client, slept):
        self._store(lab, 1e9)
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 400
        assert "labctl edit board --cycle-delay -1" in resp.get_json()["error"]
        lab.controller.power_off.assert_not_called()
        assert slept == []

    def test_view_cycle_waits_stored_delay(self, lab, client, slept):
        self._store(lab, 3.5)
        resp = client.post("/sbc/board/power/cycle")
        assert resp.status_code == 302
        assert slept == [3.5]

    def test_view_cycle_refuses_bad_stored_delay(self, lab, client, slept):
        self._store(lab, 1e9)
        resp = client.post("/sbc/board/power/cycle")
        assert resp.status_code == 302
        lab.controller.power_off.assert_not_called()
        assert slept == []


class TestCycleRuleAndOverlap:
    """One delay rule on every surface, and REST/web cycles of a board
    don't overlap (#41 review)."""

    def test_cli_negative_delay_is_an_error(self, lab):
        result = lab.run("power", "cycle", "board", "--delay", "-1", "-y")
        assert result.exit_code == 1
        assert "Error: delay must be a number of seconds from 0 to 86400" in (
            result.output
        )
        lab.controller.power_off.assert_not_called()

    def test_cli_small_delay_raised_with_warning(self, lab):
        result = lab.run("power", "cycle", "board", "--delay", "1", "-y")
        assert result.exit_code == 0, result.output
        assert "--delay 1.0s is below the default minimum of 3.0s" in result.output

    def test_api_small_delay_raised_to_default_floor(self, lab, client):
        resp = client.post(
            "/api/sbcs/board/power", json={"action": "cycle", "delay": 0.5}
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["delay_seconds"] == 3.0

    def test_api_null_delay_refused(self, lab, client):
        resp = client.post(
            "/api/sbcs/board/power", json={"action": "cycle", "delay": None}
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == (
            "delay must be a number of seconds, not null"
        )
        lab.controller.power_off.assert_not_called()

    def test_api_marks_the_cycle_in_progress_while_it_waits(
        self, lab, client, monkeypatch
    ):
        from labctl.web import api

        seen = []
        monkeypatch.setattr(
            "labctl.actuators.runtime.time.sleep",
            lambda s: seen.append(OUTLET in api._power_cycles_in_progress),
        )
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 200, resp.get_json()
        assert seen == [True]
        assert OUTLET not in api._power_cycles_in_progress

    def test_api_overlapping_cycle_refused(self, lab, client, monkeypatch):
        from labctl.web import api

        monkeypatch.setattr(api, "_power_cycles_in_progress", {OUTLET})
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 429
        assert "already in progress" in resp.get_json()["error"]
        lab.controller.power_off.assert_not_called()

    def test_api_failed_cycle_clears_the_marker(self, lab, client):
        from labctl.web import api

        lab.controller.power_off.side_effect = RuntimeError("plug gone")
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 500
        assert OUTLET not in api._power_cycles_in_progress
        # A failed cycle doesn't start the rate-limit interval.
        assert OUTLET not in api._power_cycle_times

    def test_view_overlapping_cycle_refused(self, lab, client, monkeypatch):
        from labctl.web import api

        monkeypatch.setattr(api, "_power_cycles_in_progress", {OUTLET})
        resp = client.post("/sbc/board/power/cycle")
        assert resp.status_code == 302
        lab.controller.power_off.assert_not_called()

    def test_view_cycle_rate_limited_after_rest_cycle(self, lab, client):
        assert (
            client.post("/api/sbcs/board/power", json={"action": "cycle"}).status_code
            == 200
        )
        lab.controller.power_off.reset_mock()
        client.post("/sbc/board/power/cycle")
        lab.controller.power_off.assert_not_called()


class TestOutletRules:
    """Boards on one plug outlet are cycled together (#41 review 2): the
    largest stored delay among them is the floor, and REST/web guards are
    per outlet."""

    @pytest.fixture
    def sharer(self, lab):
        other = lab.manager.create_sbc(name="board-b", project="test")
        lab.manager.assign_power_plug(other.id, PlugType.TASMOTA, "10.0.0.5", 1)
        return other

    @staticmethod
    def _store(lab, name, value):
        import sqlite3

        with sqlite3.connect(lab.db_path) as db:
            db.execute(
                "UPDATE sbcs SET power_cycle_delay_seconds = ? WHERE name = ?",
                (value, name),
            )

    @pytest.fixture
    def slept(self, lab, monkeypatch):
        waits = []
        monkeypatch.setattr("labctl.actuators.runtime.time.sleep", waits.append)
        return waits

    def test_cli_cycle_uses_sharers_larger_delay(self, lab, sharer, slept):
        self._store(lab, "board", 4.0)
        self._store(lab, "board-b", 9.0)
        result = lab.run("power", "cycle", "board", "-y")
        assert result.exit_code == 0, result.output
        assert slept == [9.0]

    def test_cli_warning_names_the_sharer(self, lab, sharer):
        self._store(lab, "board-b", 9.0)
        result = lab.run("power", "cycle", "board", "--delay", "5", "-y")
        assert result.exit_code == 0, result.output
        assert (
            "--delay 5.0s is below the minimum of 'board-b' (on the same "
            "outlet) of 9.0s"
        ) in result.output

    def test_sharers_bad_stored_delay_refuses(self, lab, sharer, slept):
        self._store(lab, "board-b", 1e9)
        result = lab.run("power", "cycle", "board", "-y")
        assert result.exit_code == 1
        assert "the cycle delay stored for 'board-b'" in result.output
        assert "('board-b' shares 'board''s power outlet)" in result.output
        lab.controller.power_off.assert_not_called()
        assert slept == []

    def test_api_cycle_uses_sharers_larger_delay(self, lab, sharer, client, slept):
        self._store(lab, "board-b", 6.0)
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["delay_seconds"] == 6.0

    def test_api_sharers_cycle_blocks_this_boards_cycle(
        self, lab, sharer, client, monkeypatch
    ):
        """board-b's cycle switches board's outlet too."""
        from labctl.web import api

        monkeypatch.setattr(api, "_power_cycles_in_progress", {OUTLET})
        resp = client.post("/api/sbcs/board-b/power", json={"action": "cycle"})
        assert resp.status_code == 429
        assert "power outlet is already in progress" in resp.get_json()["error"]
        lab.controller.power_off.assert_not_called()

    def test_api_rate_limit_is_per_outlet(self, lab, sharer, client):
        assert (
            client.post("/api/sbcs/board/power", json={"action": "cycle"}).status_code
            == 200
        )
        resp = client.post("/api/sbcs/board-b/power", json={"action": "cycle"})
        assert resp.status_code == 429
        assert "Rate limited" in resp.get_json()["error"]

    def test_api_other_outlet_not_blocked(self, lab, client, monkeypatch):
        from labctl.web import api

        monkeypatch.setattr(
            api, "_power_cycles_in_progress", {("tasmota", "10.0.0.5", 2)}
        )
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 200, resp.get_json()

    def test_api_whole_device_index_overlaps(self, lab, client, monkeypatch):
        from labctl.web import api

        monkeypatch.setattr(
            api, "_power_cycles_in_progress", {("tasmota", "10.0.0.5", 0)}
        )
        resp = client.post("/api/sbcs/board/power", json={"action": "cycle"})
        assert resp.status_code == 429

    @pytest.mark.parametrize("action", ["on", "off"])
    def test_api_on_off_refused_mid_cycle(
        self, lab, sharer, client, monkeypatch, action
    ):
        from labctl.web import api

        monkeypatch.setattr(api, "_power_cycles_in_progress", {OUTLET})
        resp = client.post("/api/sbcs/board-b/power", json={"action": action})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["switched"] is False
        assert "power outlet is in progress" in body["error"]
        lab.controller.power_on.assert_not_called()
        lab.controller.power_off.assert_not_called()

    @pytest.mark.parametrize("action", ["on", "off"])
    def test_view_on_off_refused_mid_cycle(self, lab, client, monkeypatch, action):
        from labctl.web import api

        monkeypatch.setattr(api, "_power_cycles_in_progress", {OUTLET})
        resp = client.post(f"/sbc/board/power/{action}")
        assert resp.status_code == 302
        lab.controller.power_on.assert_not_called()
        lab.controller.power_off.assert_not_called()

    def test_api_on_allowed_when_no_cycle_running(self, lab, client):
        resp = client.post("/api/sbcs/board/power", json={"action": "on"})
        assert resp.status_code == 200, resp.get_json()


class TestOutletFloorPerBoard:
    """Each board's floor is its stored delay or 3 s, and the outlet's is
    the largest (#41 review 3)."""

    @pytest.fixture
    def sharer(self, lab):
        other = lab.manager.create_sbc(name="board-b", project="test")
        lab.manager.assign_power_plug(other.id, PlugType.TASMOTA, "10.0.0.5", 1)
        return other

    def test_sharers_short_delay_doesnt_lower_this_board(
        self, lab, sharer, monkeypatch
    ):
        import sqlite3

        with sqlite3.connect(lab.db_path) as db:
            db.execute(
                "UPDATE sbcs SET power_cycle_delay_seconds = 0.5 WHERE name = 'board-b'"
            )
        waits = []
        monkeypatch.setattr("labctl.actuators.runtime.time.sleep", waits.append)
        result = lab.run("power", "cycle", "board", "-y")
        assert result.exit_code == 0, result.output
        assert waits == [3.0]


class TestNoPlugWrites:
    """Writing the card of a board with no plug is refused unless
    --no-plug-ok / no_plug_ok (#41 review 4)."""

    @pytest.fixture
    def unplugged(self, lab):
        manager = lab.manager
        sbc = manager.create_sbc(name="loose", project="test")
        device = manager.create_sdwire_device("sdw-2", "bdgrd_sdwirec_002")
        manager.assign_sdwire(sbc.id, device.id)
        ctrl = MagicMock()
        ctrl.get_block_device.return_value = "/dev/sdb"
        ctrl.flash_image.return_value = {
            "bytes_written": 3,
            "elapsed_seconds": 0.1,
            "block_device": "/dev/sdb",
        }
        ctrl.update_files.return_value = {"copied": [], "renamed": [], "deleted": ["x"]}
        with (
            patch("labctl.sdwire.controller.SDWireController", return_value=ctrl),
            patch("labctl.sdwire.SDWireController", return_value=ctrl),
            patch("time.sleep"),
        ):
            yield ctrl

    @pytest.fixture
    def image(self, tmp_path):
        path = tmp_path / "image.img"
        path.write_bytes(b"img")
        return path

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_cli_flash_refused(self, lab, unplugged, image, dry_run):
        args = ["sdwire", "flash", "loose", str(image), "-y"]
        result = lab.run(*args, *(["--dry-run"] if dry_run else []))
        assert result.exit_code == 1
        assert "'loose' has no power plug" in result.output
        assert "rerun with --no-plug-ok" in result.output
        unplugged.switch_to_host.assert_not_called()

    def test_cli_flash_no_plug_ok(self, lab, unplugged, image):
        result = lab.run("sdwire", "flash", "loose", str(image), "-y", "--no-plug-ok")
        assert result.exit_code == 0, result.output
        unplugged.flash_image.assert_called_once()

    def test_cli_flash_dry_run_says_power_off_skipped(self, lab, unplugged, image):
        result = lab.run(
            "sdwire", "flash", "loose", str(image), "--no-plug-ok", "--dry-run"
        )
        assert result.exit_code == 0, result.output
        assert "skip the power-off of loose (no power plug; --no-plug-ok)" in (
            result.output
        )

    def test_cli_update_refused(self, lab, unplugged):
        result = lab.run("sdwire", "update", "loose", "-p", "1", "-d", "x", "-y")
        assert result.exit_code == 1
        assert "'loose' has no power plug" in result.output
        unplugged.update_files.assert_not_called()

    def test_cli_update_no_plug_ok(self, lab, unplugged):
        result = lab.run(
            "sdwire", "update", "loose", "-p", "1", "-d", "x", "-y", "--no-plug-ok"
        )
        assert result.exit_code == 0, result.output
        unplugged.update_files.assert_called_once()

    def _mcp(self, lab):
        from labctl import mcp_server

        return (
            patch.object(mcp_server, "_get_manager", return_value=lab.manager),
            patch.object(mcp_server, "_check_claim", return_value=None),
        )

    def test_mcp_update_refused_and_ok(self, lab, unplugged):
        from labctl import mcp_server

        a, b = self._mcp(lab)
        with (
            a,
            b,
            patch.object(mcp_server, "_confirmation_required", return_value=False),
        ):
            result = mcp_server.sdwire_update(
                sbc_name="loose", partition=1, deletes=["x"]
            )
            assert result.startswith("Error: 'loose' has no power plug")
            assert "no_plug_ok=true" in result
            unplugged.update_files.assert_not_called()
            result = mcp_server.sdwire_update(
                sbc_name="loose", partition=1, deletes=["x"], no_plug_ok=True
            )
        assert result.startswith("Partition 1: Deleted: x"), result

    def test_mcp_flash_refused(self, lab, unplugged):
        from labctl import mcp_server

        a, b = self._mcp(lab)
        with (
            a,
            b,
            patch.object(mcp_server, "_confirmation_required", return_value=False),
        ):
            result = mcp_server.flash_image(sbc_name="loose", image_path="/nope.img")
        assert result.startswith("Error: 'loose' has no power plug")
        unplugged.flash_image.assert_not_called()

    def test_mcp_plan_refuses_without_token(self, lab, unplugged):
        from labctl import mcp_server
        from labctl.core.config import Config

        a, b = self._mcp(lab)
        with a, b, patch.object(mcp_server, "_get_config", return_value=Config()):
            result = mcp_server.flash_image(sbc_name="loose", image_path="/x.img")
            assert result.startswith("Error: 'loose' has no power plug")
            assert "confirm_token" not in result
            plan = json.loads(
                mcp_server.sdwire_update(
                    sbc_name="loose", partition=1, deletes=["x"], no_plug_ok=True
                )
            )
        assert plan["will_do"] == {
            "power_off": "not switched off: no power plug (no_plug_ok)"
        }
