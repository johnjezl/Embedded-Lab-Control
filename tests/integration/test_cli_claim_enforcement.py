"""CLI claim enforcement (D013 part 2): board-changing commands refuse on
another user's claim unless --override-claim, which is logged."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.cli import _cli_session_id, main
from labctl.core import audit
from labctl.core.models import ChannelState, DriverName, PlugType, PortType, ShapeMode


@pytest.fixture
def lab(tmp_path):
    """Throwaway config/DB: pi-a claimed by another user, pi-b and pi-c free."""
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
    for name in ("pi-a", "pi-b", "pi-c"):
        manager.create_sbc(name=name, project="test")
    manager.assign_power_plug(
        manager.get_sbc_by_name("pi-a").id, PlugType.TASMOTA, "10.0.0.5", 1
    )
    manager.assign_power_plug(
        manager.get_sbc_by_name("pi-c").id, PlugType.TASMOTA, "10.0.0.6", 1
    )
    manager.claim_sbc(
        sbc_name="pi-a",
        agent_name="agent-x",
        session_id="mcp-http:1:agent-x",
        session_kind="mcp-http",
        duration_seconds=600,
        reason="testing",
        owner="agent-x",
    )
    runner = CliRunner()

    def run(*args, **kwargs):
        return runner.invoke(main, ["-c", str(config_path), *args], **kwargs)

    return run, manager, config_path


@pytest.fixture
def controller():
    """Mock power controller, so allowed commands touch no hardware."""
    ctrl = MagicMock()
    ctrl.power_on.return_value = True
    ctrl.power_off.return_value = True
    with patch("labctl.cli.PowerController.from_plug", return_value=ctrl):
        yield ctrl


def _overrides(manager, sbc_name):
    return [
        e
        for e in audit.query_events(manager.db, sbc=sbc_name)
        if e["action"] == "claim_override"
    ]


def _share_plug(manager, sbc_name, address="10.0.0.5", index=1):
    manager.assign_power_plug(
        manager.get_sbc_by_name(sbc_name).id, PlugType.TASMOTA, address, index
    )


class TestOperatingCommands:
    def test_power_on_refused_on_another_users_claim(self, lab, controller):
        run, manager, _ = lab
        result = run("power", "on", "pi-a")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert "--override-claim" in result.output
        assert "request-release pi-a" in result.output
        controller.power_on.assert_not_called()

    def test_override_acts_and_is_logged(self, lab, controller):
        run, manager, _ = lab
        result = run("power", "on", "pi-a", "--override-claim")
        assert result.exit_code == 0, result.output
        controller.power_on.assert_called_once()
        events = _overrides(manager, "pi-a")
        assert len(events) == 1
        assert events[0]["actor"].startswith("cli:")
        # The claim stays with its holder.
        assert manager.get_active_claim("pi-a").agent_name == "agent-x"

    def test_unclaimed_board_unaffected(self, lab, controller):
        run, manager, _ = lab
        result = run("power", "on", "pi-c")
        assert result.exit_code == 0, result.output
        controller.power_on.assert_called_once()
        assert _overrides(manager, "pi-c") == []

    def test_own_cli_claim_allowed(self, lab, controller):
        run, manager, _ = lab
        assert run("claim", "pi-c", "-r", "mine").exit_code == 0
        result = run("power", "on", "pi-c")
        assert result.exit_code == 0, result.output
        controller.power_on.assert_called_once()
        assert _overrides(manager, "pi-c") == []

    def test_board_sharing_a_claimed_boards_plug_refused(self, lab, controller):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        result = run("power", "on", "pi-b")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert "shares hardware with 'pi-b'" in result.output
        controller.power_on.assert_not_called()

    def test_claims_disabled_checks_nothing(self, lab, controller, tmp_path):
        run, manager, config_path = lab
        config_path.write_text(
            config_path.read_text().replace("enabled: true", "enabled: false")
        )
        result = run("power", "on", "pi-a")
        assert result.exit_code == 0, result.output
        controller.power_on.assert_called_once()

    def test_unknown_board_reports_not_found(self, lab):
        run, _, _ = lab
        result = run("power", "on", "nope")
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_serial_send_resolves_port_alias_to_board(self, lab):
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id,
            PortType.CONSOLE,
            "/dev/lab/port-a",
            tcp_port=4001,
            alias="pi-a-con",
        )
        with patch("labctl.serial.capture.send_serial_data") as send:
            result = run("serial", "send", "pi-a-con", "help", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed" in result.output
        send.assert_not_called()


class TestConfirmationOrder:
    def test_refuses_before_asking(self, lab, controller, monkeypatch):
        run, _, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("power", "off", "pi-a", input="y\n")
        assert result.exit_code == 1
        assert "Continue?" not in result.output
        assert "claimed by 'agent-x'" in result.output
        controller.power_off.assert_not_called()

    def test_override_logged_only_after_yes(self, lab, controller, monkeypatch):
        run, manager, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("power", "off", "pi-a", "--override-claim", input="n\n")
        assert result.exit_code != 0
        assert "Continue?" in result.output
        assert _overrides(manager, "pi-a") == []
        controller.power_off.assert_not_called()

        result = run("power", "off", "pi-a", "--override-claim", input="y\n")
        assert result.exit_code == 0, result.output
        assert len(_overrides(manager, "pi-a")) == 1
        controller.power_off.assert_called_once()


class TestDryRun:
    def test_dry_run_refused_without_override(self, lab, controller):
        run, _, _ = lab
        result = run("power", "cycle", "pi-a", "--dry-run")
        assert result.exit_code == 1
        assert "claimed by 'agent-x'" in result.output

    def test_dry_run_reports_override_without_logging(self, lab, controller):
        run, manager, _ = lab
        result = run("power", "cycle", "pi-a", "--dry-run", "--override-claim")
        assert result.exit_code == 0, result.output
        assert "would override 'agent-x's claim on 'pi-a'" in result.output
        assert "Dry run: would power-cycle" in result.output
        assert _overrides(manager, "pi-a") == []
        controller.power_off.assert_not_called()


class TestConfigurationCommands:
    def test_edit_refused(self, lab):
        run, manager, _ = lab
        result = run("edit", "pi-a", "--description", "mine now")
        assert result.exit_code == 1
        assert "claimed by 'agent-x'" in result.output
        assert manager.get_sbc_by_name("pi-a").description != "mine now"

    def test_edit_of_a_sharing_board_allowed(self, lab):
        """Changing pi-b's own records doesn't touch pi-a's hardware."""
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        result = run("edit", "pi-b", "--description", "fine")
        assert result.exit_code == 0, result.output

    def test_plug_assign_of_a_claimed_boards_outlet_refused(self, lab):
        run, manager, _ = lab
        result = run("plug", "assign", "pi-b", "tasmota", "10.0.0.5")
        assert result.exit_code == 1
        assert "shares hardware with 'pi-b'" in result.output
        assert manager.get_sbc_by_name("pi-b").power_plug is None

        result = run(
            "plug", "assign", "pi-b", "tasmota", "10.0.0.5", "--override-claim"
        )
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-b").power_plug is not None
        assert len(_overrides(manager, "pi-a")) == 1

    def test_plug_assign_other_outlet_allowed(self, lab):
        run, manager, _ = lab
        result = run("plug", "assign", "pi-b", "tasmota", "10.0.0.5", "-i", "2")
        assert result.exit_code == 0, result.output

    def test_port_assign_of_a_claimed_boards_adapter_refused(self, lab):
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id, PortType.CONSOLE, "/dev/lab/port-a"
        )
        result = run("port", "assign", "pi-b", "console", "/dev/lab/port-a")
        assert result.exit_code == 1
        assert "shares hardware with 'pi-b'" in result.output

    def test_sdwire_assign_of_a_claimed_boards_device_refused(self, lab):
        run, manager, _ = lab
        device = manager.create_sdwire_device("sw-1", "SN1", "sdwirec")
        manager.assign_sdwire(manager.get_sbc_by_name("pi-a").id, device.id)
        result = run("sdwire", "assign", "pi-b", "sw-1")
        assert result.exit_code == 1
        assert "shares hardware with 'pi-b'" in result.output

    def test_plug_remove_refused(self, lab):
        run, manager, _ = lab
        result = run("plug", "remove", "pi-a", "--yes")
        assert result.exit_code == 1
        assert manager.get_sbc_by_name("pi-a").power_plug is not None

    def test_serial_rename_of_a_claimed_boards_adapter_refused(self, lab):
        run, manager, _ = lab
        device = manager.create_serial_device("port-a", "1-1.1")
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id,
            PortType.CONSOLE,
            "/dev/lab/port-a",
            serial_device_id=device.id,
        )
        result = run("serial", "rename", "port-a", "port-z")
        assert result.exit_code == 1
        assert "'pi-a' is claimed" in result.output
        assert manager.get_serial_device_by_name("port-a") is not None


class TestRemove:
    def test_refused_without_override(self, lab):
        run, manager, _ = lab
        result = run("remove", "pi-a", "--yes")
        assert result.exit_code == 1
        assert "--override-claim" in result.output
        assert manager.get_sbc_by_name("pi-a") is not None

    def test_override_removes_and_logs(self, lab):
        run, manager, _ = lab
        result = run("remove", "pi-a", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-a") is None
        assert len(_overrides(manager, "pi-a")) == 1

    def test_force_still_overrides(self, lab):
        run, manager, _ = lab
        result = run("remove", "pi-a", "--yes", "--force")
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-a") is None

    def test_own_claim_released_then_removed(self, lab):
        run, manager, _ = lab
        assert run("claim", "pi-c", "-r", "mine").exit_code == 0
        result = run("remove", "pi-c", "--yes")
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-c") is None
        released = [
            e
            for e in audit.query_events(manager.db, sbc="pi-c")
            if e["action"] == "release"
        ]
        assert released

    def test_refuses_before_asking(self, lab):
        run, manager, _ = lab
        result = run("remove", "pi-a", input="y\n")
        assert result.exit_code == 1
        assert "Remove SBC" not in result.output
        assert manager.get_sbc_by_name("pi-a") is not None


class TestActuators:
    @pytest.fixture
    def relay(self, lab):
        _, manager, _ = lab
        actuator = manager.create_actuator(
            "relay-1", DriverName.LCUS1_SERIAL, device_path="/dev/ttyUSB-relay-1"
        )
        manager.add_actuator_channel(actuator.id, 1, default_state=ChannelState.OPEN)
        manager.add_actuator_channel(actuator.id, 2, default_state=ChannelState.OPEN)
        manager.create_binding(
            manager.get_sbc_by_name("pi-a").id,
            "reset",
            manager.get_actuator_channel(actuator.id, 1).id,
            shape_mode=ShapeMode.MOMENTARY,
            shape_active=ChannelState.CLOSED,
            momentary_pulse_ms=100,
        )
        return actuator

    def test_actuator_set_on_a_claimed_boards_channel_refused(self, lab, relay):
        run, _, _ = lab
        with patch("labctl.actuators.get_driver") as get_driver:
            result = run("actuator", "set", "relay-1", "1", "closed", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed" in result.output
        get_driver.assert_not_called()

    def test_actuator_set_refuses_before_asking(self, lab, relay, monkeypatch):
        run, _, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("actuator", "set", "relay-1", "1", "closed", input="y\n")
        assert result.exit_code == 1
        assert "Continue?" not in result.output

    def test_bind_to_a_claimed_boards_channel_refused(self, lab, relay):
        run, manager, _ = lab
        result = run(
            "bind",
            "pi-b",
            "reset",
            "--actuator",
            "relay-1",
            "--channel",
            "1",
            "--mode",
            "momentary",
            "--active-when",
            "closed",
            "--pulse-ms",
            "100",
        )
        assert result.exit_code == 1, result.output
        assert "shares hardware with 'pi-b'" in result.output

    def test_actuator_remove_refused(self, lab, relay):
        run, manager, _ = lab
        result = run("actuator", "remove", "relay-1", "--yes")
        assert result.exit_code == 1
        assert manager.get_actuator_by_name("relay-1") is not None

    def test_bindings_press_refused(self, lab, relay):
        run, _, _ = lab
        with patch("labctl.actuators.runtime.press_binding") as press:
            result = run("bindings", "press", "pi-a", "reset", "--yes")
        assert result.exit_code == 1
        press.assert_not_called()


class TestPowerAll:
    def test_skips_claimed_boards(self, lab, controller):
        run, manager, _ = lab
        result = run("power-all", "on", "--yes")
        assert result.exit_code == 0, result.output
        assert "skipped: claimed by 'agent-x'" in result.output
        assert "(1 skipped: claimed by another user)" in result.output
        assert controller.power_on.call_count == 1  # pi-c only

    def test_override_powers_all_and_logs(self, lab, controller):
        run, manager, _ = lab
        result = run("power-all", "on", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert controller.power_on.call_count == 2
        assert len(_overrides(manager, "pi-a")) == 1

    def test_every_board_claimed(self, lab, controller):
        run, manager, _ = lab
        manager.remove_power_plug(manager.get_sbc_by_name("pi-c").id)
        result = run("power-all", "off", "--yes")
        assert result.exit_code == 1
        assert "every board is claimed" in result.output
        controller.power_off.assert_not_called()


class TestImport:
    def _write(self, tmp_path, sbcs):
        path = tmp_path / "import.json"
        path.write_text(json.dumps({"sbcs": sbcs}))
        return path

    def test_update_of_a_claimed_board_refused_before_any_change(self, lab, tmp_path):
        run, manager, _ = lab
        path = self._write(
            tmp_path,
            [
                {"name": "pi-new"},
                {"name": "pi-a", "description": "taken over"},
            ],
        )
        result = run("import", str(path), "--update")
        assert result.exit_code == 1
        assert "'pi-a' is claimed" in result.output
        assert manager.get_sbc_by_name("pi-new") is None

    def test_skipped_claimed_board_doesnt_refuse(self, lab, tmp_path):
        run, manager, _ = lab
        path = self._write(tmp_path, [{"name": "pi-a"}, {"name": "pi-new"}])
        result = run("import", str(path))
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-new") is not None

    def test_new_board_on_a_claimed_boards_plug_refused(self, lab, tmp_path):
        run, manager, _ = lab
        path = self._write(
            tmp_path,
            [
                {
                    "name": "pi-new",
                    "power_plug": {"type": "tasmota", "address": "10.0.0.5"},
                }
            ],
        )
        result = run("import", str(path))
        assert result.exit_code == 1
        assert "shares hardware with 'pi-new'" in result.output
        assert manager.get_sbc_by_name("pi-new") is None

        result = run("import", str(path), "--override-claim")
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-new") is not None


class TestClaim:
    def test_claim_of_a_board_sharing_a_claimed_boards_hardware_refused(self, lab):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        result = run("claim", "pi-b", "-r", "mine")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert "shares hardware with 'pi-b'" in result.output
        assert "force-release pi-a" in result.output
        assert manager.get_active_claim("pi-b") is None

    def test_cli_session_id_shape(self):
        assert _cli_session_id().startswith("cli-")
