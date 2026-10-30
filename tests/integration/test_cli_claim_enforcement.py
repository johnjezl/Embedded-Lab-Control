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
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert manager.get_sbc_by_name("pi-a").power_plug is not None

    def test_serial_rename_not_checked(self, lab):
        """Lab-wide serial setup (rename/udev/ser2net) isn't claim-checked."""
        run, manager, _ = lab
        device = manager.create_serial_device("port-a", "1-1.1")
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id,
            PortType.CONSOLE,
            "/dev/lab/port-a",
            serial_device_id=device.id,
        )
        result = run("serial", "rename", "port-a", "port-z")
        assert result.exit_code == 0, result.output
        assert manager.get_serial_device_by_name("port-z") is not None


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
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert manager.get_actuator_by_name("relay-1") is not None

    def test_bindings_press_refused(self, lab, relay):
        run, _, _ = lab
        with patch("labctl.actuators.runtime.press_binding") as press:
            result = run("bindings", "press", "pi-a", "reset", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
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


class TestRoundOneFixes:
    def test_claim_names_the_boards_own_holder_first(self, lab):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        manager.claim_sbc(
            sbc_name="pi-b",
            agent_name="agent-y",
            session_id="mcp-http:1:agent-y",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="mine",
            owner="agent-y",
        )
        result = run("claim", "pi-b", "-r", "x")
        assert result.exit_code == 1
        assert "'pi-b' is claimed by 'agent-y'" in result.output
        assert "force-release pi-b" in result.output

    def test_import_with_unreadable_plug_index_doesnt_crash(self, lab, tmp_path):
        run, manager, _ = lab
        path = tmp_path / "import.json"
        path.write_text(
            json.dumps(
                {
                    "sbcs": [
                        {
                            "name": "pi-new",
                            "power_plug": {
                                "type": "tasmota",
                                "address": "10.0.0.5",
                                "index": None,
                            },
                        }
                    ]
                }
            )
        )
        result = run("import", str(path))
        # Unreadable index: checked against every outlet on the address.
        assert result.exit_code == 1
        assert not isinstance(result.exception, TypeError)
        assert "'pi-a' is claimed" in result.output

    def test_prompt_says_which_claim_is_overridden(self, lab, controller, monkeypatch):
        run, _, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("power", "off", "pi-a", "--override-claim", input="n\n")
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert result.output.index("This overrides") < result.output.index("Continue?")

    def test_remove_prompt_says_which_claim_is_overridden(self, lab):
        run, manager, _ = lab
        result = run("remove", "pi-a", "--override-claim", input="n\n")
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert manager.get_sbc_by_name("pi-a") is not None
        assert _overrides(manager, "pi-a") == []

    def test_serial_capture_not_checked(self, lab):
        """Reading a console is unchecked, like `console` and `connect`."""
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id,
            PortType.CONSOLE,
            "/dev/lab/port-a",
            tcp_port=4001,
        )
        captured = MagicMock(output="", pattern_matched=False, lines=0)
        captured.elapsed_seconds = 0.0
        with patch(
            "labctl.serial.capture.capture_serial_output", return_value=captured
        ) as capture:
            result = run("serial", "capture", "pi-a", "-t", "0.1")
        assert result.exit_code == 0, result.output
        capture.assert_called_once()


# Every decorated command refuses on pi-a's claim before acting. Arguments
# beyond the board are enough to get past click's own parsing.
REFUSED_COMMANDS = [
    ["power", "on", "pi-a"],
    ["power", "off", "pi-a", "--yes"],
    ["power", "cycle", "pi-a", "--yes"],
    ["sdwire", "unassign", "pi-a", "--yes"],
    ["sdwire", "dut", "pi-a"],
    ["sdwire", "host", "pi-a", "--yes"],
    ["sdwire", "flash", "pi-a", "{image}", "--yes"],
    ["sdwire", "update", "pi-a", "-p", "1", "-d", "x", "--yes"],
    ["sdwire", "ls", "pi-a", "-p", "1"],
    ["sdwire", "cat", "pi-a", "-p", "1", "--path", "/x"],
    ["sdwire", "info", "pi-a"],
    ["port", "remove", "pi-a", "console", "--yes"],
    ["network", "set", "pi-a", "ethernet", "10.0.0.9"],
    ["network", "remove", "pi-a", "ethernet", "--yes"],
    ["plug", "remove", "pi-a", "--yes"],
    ["unbind", "pi-a", "reset", "--yes"],
    ["bindings", "actuate", "pi-a", "reset", "--yes"],
    ["bindings", "release", "pi-a", "reset", "--yes"],
    ["bindings", "press", "pi-a", "reset", "--yes"],
    ["enter-recovery", "pi-a", "--yes"],
    ["exit-recovery", "pi-a", "--yes"],
    ["boot-test", "pi-a", "-e", "login", "--no-deploy", "--yes"],
    ["edit", "pi-a", "--project", "x"],
]


@pytest.mark.parametrize("args", REFUSED_COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_command_refused_on_another_users_claim(lab, controller, args, tmp_path):
    run, manager, _ = lab
    image = tmp_path / "img.img"
    image.write_bytes(b"")
    args = [str(image) if a == "{image}" else a for a in args]
    before = manager.get_sbc_by_name("pi-a")
    result = run(*args)
    assert result.exit_code == 1, result.output
    assert "'pi-a' is claimed by 'agent-x'" in result.output
    controller.power_on.assert_not_called()
    controller.power_off.assert_not_called()
    after = manager.get_sbc_by_name("pi-a")
    assert after.project == before.project
    assert after.power_plug is not None


@pytest.fixture
def grace_lab(lab):
    """pi-a's claim has expired but is inside a 60 s grace period."""
    run, manager, config_path = lab
    config_path.write_text(
        config_path.read_text().replace(
            "grace_period_seconds: 0", "grace_period_seconds: 60"
        )
    )
    _expire_claims(manager, seconds_ago=5)
    return lab


def _expire_claims(manager, seconds_ago):
    """Move open claims' deadlines into the past (local time, as stored)."""
    from datetime import datetime, timedelta

    manager.db.execute_modify(
        "UPDATE claims SET expires_at = ? WHERE released_at IS NULL",
        (manager._fmt_ts(datetime.now() - timedelta(seconds=seconds_ago)),),
    )


class TestRoundTwoFixes:
    def test_reclaiming_your_own_board_points_at_renew(self, lab):
        run, _, _ = lab
        assert run("claim", "pi-c", "-r", "mine").exit_code == 0
        result = run("claim", "pi-c", "-r", "again")
        assert result.exit_code == 1
        assert "you already hold the claim on 'pi-c'" in result.output
        assert "labctl renew pi-c" in result.output
        assert "force-release" not in result.output

    def test_claim_in_grace_period_says_so(self, grace_lab):
        run, manager, _ = grace_lab
        result = run("claim", "pi-a", "-r", "x")
        assert result.exit_code == 1
        assert "grace period" in result.output
        assert "request-release" not in result.output

    def test_remove_hint_without_claims_doesnt_promise_a_log(self, lab):
        run, manager, config_path = lab
        config_path.write_text(
            config_path.read_text().replace("enabled: true", "enabled: false")
        )
        result = run("remove", "pi-a", "--yes")
        assert result.exit_code == 1
        assert "--override-claim" in result.output
        assert "recorded in the activity log" not in result.output

    def test_actuator_set_prompt_names_the_overridden_claim(self, lab, monkeypatch):
        run, manager, _ = lab
        actuator = manager.create_actuator(
            "relay-1", DriverName.LCUS1_SERIAL, device_path="/dev/ttyUSB-r1"
        )
        manager.add_actuator_channel(actuator.id, 1, default_state=ChannelState.OPEN)
        manager.create_binding(
            manager.get_sbc_by_name("pi-a").id,
            "reset",
            manager.get_actuator_channel(actuator.id, 1).id,
            shape_mode=ShapeMode.MOMENTARY,
            shape_active=ChannelState.CLOSED,
            momentary_pulse_ms=100,
        )
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run(
            "actuator",
            "set",
            "relay-1",
            "1",
            "closed",
            "--override-claim",
            input="n\n",
        )
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert _overrides(manager, "pi-a") == []

        result = run("actuator", "remove", "relay-1", "--override-claim", input="y\n")
        assert result.exit_code == 0, result.output
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert manager.get_actuator_by_name("relay-1") is None
        assert len(_overrides(manager, "pi-a")) == 1

    def test_power_all_skips_a_board_claimed_after_the_listing(
        self, lab, controller, monkeypatch
    ):
        import labctl.cli as cli

        run, manager, _ = lab
        # The listing sees no claims; the per-board check before acting does.
        real, seen = cli._all_conflicts, set()

        def claimed_after_listing(ctx, sbc_name, **kw):
            if sbc_name not in seen:
                seen.add(sbc_name)
                return []
            return real(ctx, sbc_name, **kw)

        monkeypatch.setattr(cli, "_all_conflicts", claimed_after_listing)
        result = run("power-all", "on", "--yes")
        assert result.exit_code == 0, result.output
        assert "pi-a: SKIPPED - claimed by 'agent-x'" in result.output
        assert "(1 skipped: claimed by another user)" in result.output
        assert controller.power_on.call_count == 1
        assert _overrides(manager, "pi-a") == []

    def test_import_serial_port_on_a_claimed_boards_adapter_refused(
        self, lab, tmp_path
    ):
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id, PortType.CONSOLE, "/dev/lab/port-a"
        )
        path = tmp_path / "i.json"
        path.write_text(
            json.dumps(
                {
                    "sbcs": [
                        {
                            "name": "pi-new",
                            "serial_ports": [
                                {"type": "console", "device": "/dev/lab/port-a"}
                            ],
                        }
                    ]
                }
            )
        )
        result = run("import", str(path))
        assert result.exit_code == 1
        assert "shares hardware with 'pi-new'" in result.output
        assert manager.get_sbc_by_name("pi-new") is None

    def test_import_sdwire_of_a_claimed_board_refused(self, lab, tmp_path):
        run, manager, _ = lab
        device = manager.create_sdwire_device("sw-1", "SN1", "sdwirec")
        manager.assign_sdwire(manager.get_sbc_by_name("pi-a").id, device.id)
        path = tmp_path / "i.json"
        path.write_text(
            json.dumps({"sbcs": [{"name": "pi-new", "sdwire": {"name": "sw-1"}}]})
        )
        result = run("import", str(path))
        assert result.exit_code == 1
        assert "shares hardware with 'pi-new'" in result.output
        assert manager.get_sbc_by_name("pi-new") is None

    def test_import_update_override_logs(self, lab, tmp_path):
        run, manager, _ = lab
        path = tmp_path / "i.json"
        path.write_text(json.dumps({"sbcs": [{"name": "pi-a", "project": "new"}]}))
        result = run("import", str(path), "--update", "--override-claim")
        assert result.exit_code == 0, result.output
        assert manager.get_sbc_by_name("pi-a").project == "new"
        assert len(_overrides(manager, "pi-a")) == 1


class TestRoundThreeFixes:
    def test_stale_unswept_claim_doesnt_block_claim(self, grace_lab):
        run, manager, _ = grace_lab
        _expire_claims(manager, seconds_ago=3600)  # long past the 60 s grace
        result = run("claim", "pi-a", "-r", "x")
        assert result.exit_code == 0, result.output
        assert manager.get_active_claim("pi-a").agent_name != "agent-x"

    def test_prompt_names_every_overridden_claim(self, lab, controller, monkeypatch):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        manager.claim_sbc(
            sbc_name="pi-b",
            agent_name="agent-y",
            session_id="mcp-http:1:agent-y",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="r",
            owner="agent-y",
        )
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("power", "off", "pi-a", "--override-claim", input="y\n")
        assert result.exit_code == 0, result.output
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert "This overrides 'agent-y's claim on 'pi-b'." in result.output
        assert len(_overrides(manager, "pi-a")) == 1
        assert len(_overrides(manager, "pi-b")) == 1

    def test_power_all_lists_every_holder(self, lab, controller):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        manager.claim_sbc(
            sbc_name="pi-b",
            agent_name="agent-y",
            session_id="mcp-http:1:agent-y",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="r",
            owner="agent-y",
        )
        result = run("power-all", "on", "--yes")
        assert result.exit_code == 0, result.output  # pi-c is free
        assert "1/3 SBCs powered ON (2 skipped" in result.output
        assert (
            "pi-a (tasmota @ 10.0.0.5) [skipped: claimed by 'agent-x', "
            "'agent-y' on 'pi-b']" in result.output
        )
        assert (
            "pi-b (tasmota @ 10.0.0.5) [skipped: claimed by 'agent-y', "
            "'agent-x' on 'pi-a']" in result.output
        )


def _claim_pi_b_sharing_pi_as_plug(manager):
    _share_plug(manager, "pi-b")
    manager.claim_sbc(
        sbc_name="pi-b",
        agent_name="agent-y",
        session_id="mcp-http:1:agent-y",
        session_kind="mcp-http",
        duration_seconds=600,
        reason="r",
        owner="agent-y",
    )


class TestRoundFourFixes:
    def test_override_without_prompt_names_the_claims(self, lab, controller):
        run, manager, _ = lab
        _claim_pi_b_sharing_pi_as_plug(manager)
        result = run("power", "off", "pi-a", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert "Overriding 'agent-x's claim on 'pi-a'." in result.output
        assert "Overriding 'agent-y's claim on 'pi-b'." in result.output

    def test_assign_override_names_the_claim(self, lab):
        run, _, _ = lab
        result = run(
            "plug", "assign", "pi-b", "tasmota", "10.0.0.5", "--override-claim"
        )
        assert result.exit_code == 0, result.output
        assert "Overriding 'agent-x's claim on 'pi-a'." in result.output

    def test_prompted_override_announced_once(self, lab, controller, monkeypatch):
        run, _, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("power", "off", "pi-a", "--override-claim", input="y\n")
        assert result.exit_code == 0, result.output
        assert result.output.count("agent-x's claim") == 1

    def test_power_all_records_each_override_once(self, lab, controller):
        run, manager, _ = lab
        _claim_pi_b_sharing_pi_as_plug(manager)
        result = run("power-all", "on", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert len(_overrides(manager, "pi-a")) == 1
        assert len(_overrides(manager, "pi-b")) == 1


class TestRoundFiveFixes:
    def test_claim_made_while_prompt_waits_is_named(self, lab, controller, monkeypatch):
        run, manager, _ = lab
        _share_plug(manager, "pi-b")
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)

        def confirm_after_a_new_claim(*args, **kwargs):
            manager.claim_sbc(
                sbc_name="pi-b",
                agent_name="agent-y",
                session_id="mcp-http:1:agent-y",
                session_kind="mcp-http",
                duration_seconds=600,
                reason="r",
                owner="agent-y",
            )
            return True

        monkeypatch.setattr("labctl.cli.click.confirm", confirm_after_a_new_claim)
        result = run("power", "off", "pi-a", "--override-claim")
        assert result.exit_code == 0, result.output
        assert "This overrides 'agent-x's claim on 'pi-a'." in result.output
        assert "Overriding 'agent-y's claim on 'pi-b'." in result.output
        assert "Overriding 'agent-x's" not in result.output

    def test_power_all_names_a_claim_made_after_the_listing(
        self, lab, controller, monkeypatch
    ):
        import labctl.cli as cli

        run, manager, _ = lab
        real, seen = cli._all_conflicts, set()

        def claimed_after_listing(ctx, sbc_name, **kw):
            if sbc_name not in seen:
                seen.add(sbc_name)
                return []
            return real(ctx, sbc_name, **kw)

        monkeypatch.setattr(cli, "_all_conflicts", claimed_after_listing)
        result = run("power-all", "on", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert "Overriding 'agent-x's claim on 'pi-a'." in result.output
        assert len(_overrides(manager, "pi-a")) == 1

    def test_power_all_heartbeats_own_claim(self, lab, controller):
        run, manager, _ = lab
        assert run("claim", "pi-c", "-r", "mine", "-d", "5m").exit_code == 0
        before = manager.get_active_claim("pi-c").last_activity
        import time

        time.sleep(0.05)
        assert run("power-all", "on", "--yes").exit_code == 0
        assert manager.get_active_claim("pi-c").last_activity > before


class TestRoundSixFixes:
    def test_port_assign_refuses_another_boards_tcp_port(self, lab):
        """A shared ser2net TCP port would reach the claimed board's console."""
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id,
            PortType.CONSOLE,
            "/dev/lab/port-a",
            tcp_port=4001,
        )
        result = run(
            "port", "assign", "pi-b", "console", "/dev/lab/port-b", "--tcp-port", "4001"
        )
        assert result.exit_code == 1
        assert "TCP port 4001 is already in use" in result.output
        assert manager.get_sbc_by_name("pi-b").console_port is None

    def test_reassigning_a_ports_own_tcp_port_allowed(self, lab):
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-c").id,
            PortType.CONSOLE,
            "/dev/lab/port-c",
            tcp_port=4003,
        )
        result = run(
            "port",
            "assign",
            "pi-c",
            "console",
            "/dev/lab/port-c2",
            "--tcp-port",
            "4003",
        )
        assert result.exit_code == 0, result.output
        console = manager.get_sbc_by_name("pi-c").console_port
        assert (console.device_path, console.tcp_port) == ("/dev/lab/port-c2", 4003)


def _duplicate_tcp_ports(manager):
    """pi-a and pi-b consoles on one TCP port, as saved before assignment
    refused duplicates."""
    manager.assign_serial_port(
        manager.get_sbc_by_name("pi-b").id,
        PortType.CONSOLE,
        "/dev/lab/port-b",
        tcp_port=4000,
    )
    manager.assign_serial_port(
        manager.get_sbc_by_name("pi-a").id,
        PortType.CONSOLE,
        "/dev/lab/port-a",
        tcp_port=4001,
    )
    manager.db.execute_modify(
        "UPDATE serial_ports SET tcp_port = 4000 WHERE tcp_port = 4001"
    )


class TestRoundSevenFixes:
    def test_existing_shared_tcp_port_counts_as_shared_hardware(self, lab):
        run, manager, _ = lab
        _duplicate_tcp_ports(manager)
        with patch("labctl.serial.capture.send_serial_data") as send:
            result = run("serial", "send", "pi-b", "hi", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert "shares hardware with 'pi-b'" in result.output
        send.assert_not_called()

    def test_tcp_check_sees_every_row(self, lab):
        """pi-b's own row comes first, but pi-a's row has the port too."""
        run, manager, _ = lab
        _duplicate_tcp_ports(manager)
        with pytest.raises(ValueError, match="TCP port 4000 is already in use"):
            manager.assign_serial_port(
                manager.get_sbc_by_name("pi-b").id,
                PortType.CONSOLE,
                "/dev/lab/port-b2",
                tcp_port=4000,
            )

    def test_import_warns_on_a_taken_tcp_port_and_carries_on(self, lab, tmp_path):
        run, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-c").id,
            PortType.CONSOLE,
            "/dev/lab/port-c",
            tcp_port=4003,
        )
        path = tmp_path / "i.json"
        path.write_text(
            json.dumps(
                {
                    "sbcs": [
                        {
                            "name": "pi-new",
                            "serial_ports": [
                                {
                                    "type": "console",
                                    "device": "/dev/lab/port-n",
                                    "tcp_port": 4003,
                                }
                            ],
                        },
                        {"name": "pi-newer"},
                    ]
                }
            )
        )
        result = run("import", str(path))
        assert result.exit_code == 0, result.output
        assert "TCP port 4003 is already in use" in result.output
        assert manager.get_sbc_by_name("pi-newer") is not None


class TestRoundEightFixes:
    def test_tcp_port_zero_refused(self, lab):
        run, manager, _ = lab
        result = run(
            "port", "assign", "pi-b", "console", "/dev/lab/port-b", "--tcp-port", "0"
        )
        assert result.exit_code == 2
        with pytest.raises(ValueError, match="out of range"):
            manager.assign_serial_port(
                manager.get_sbc_by_name("pi-b").id,
                PortType.CONSOLE,
                "/dev/lab/port-b",
                tcp_port=0,
            )


class TestRoundNineFixes:
    def test_auto_assign_past_65535_refused(self, lab):
        _, manager, _ = lab
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-b").id,
            PortType.CONSOLE,
            "/dev/lab/port-b",
            tcp_port=65535,
        )
        with pytest.raises(ValueError, match="No TCP port left"):
            manager.assign_serial_port(
                manager.get_sbc_by_name("pi-c").id, PortType.CONSOLE, "/dev/lab/c"
            )

    def test_tcp_port_as_text(self, lab):
        _, manager, _ = lab
        sbc_id = manager.get_sbc_by_name("pi-b").id
        port = manager.assign_serial_port(
            sbc_id, PortType.CONSOLE, "/dev/lab/port-b", tcp_port="4005"
        )
        assert port.tcp_port == 4005
        for bad in ("four", "4000.0", 4000.9, True, float("inf")):
            with pytest.raises(ValueError, match="not a whole number"):
                manager.assign_serial_port(
                    sbc_id, PortType.DEBUG, "/dev/lab/port-b2", tcp_port=bad
                )

    def test_reassigning_the_highest_port_keeps_it(self, lab):
        _, manager, _ = lab
        sbc_id = manager.get_sbc_by_name("pi-b").id
        manager.assign_serial_port(
            sbc_id, PortType.CONSOLE, "/dev/lab/port-b", tcp_port=65535
        )
        port = manager.assign_serial_port(sbc_id, PortType.CONSOLE, "/dev/lab/new")
        assert (port.device_path, port.tcp_port) == ("/dev/lab/new", 65535)


class TestSerialAdapters:
    """`serial remove` and `serial udev --install` are claim-checked (#41)."""

    @pytest.fixture
    def unlinked(self, lab, monkeypatch):
        """pi-a's console is assigned by /dev/ttyUSB7, the node behind
        /dev/lab/port-a, so the port isn't linked to the registration."""
        _, manager, _ = lab
        device = manager.create_serial_device("port-a", "1-1.1")
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id, PortType.CONSOLE, "/dev/ttyUSB7"
        )
        real = __import__("os").path.realpath
        nodes = {"/dev/lab/port-a": "/dev/ttyUSB7", "/dev/ttyUSB7": "/dev/ttyUSB7"}
        monkeypatch.setattr(
            "labctl.core.manager.os.path.realpath", lambda p: nodes.get(p) or real(p)
        )
        return device

    def test_serial_remove_refused_for_a_claimed_boards_adapter(self, lab, unlinked):
        run, manager, _ = lab
        result = run("serial", "remove", "port-a", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert "--override-claim" in result.output
        assert manager.get_serial_device_by_name("port-a") is not None

    def test_serial_remove_override_removes_and_logs(self, lab, unlinked):
        run, manager, _ = lab
        result = run("serial", "remove", "port-a", "--yes", "--override-claim")
        assert result.exit_code == 0, result.output
        assert manager.get_serial_device_by_name("port-a") is None
        assert len(_overrides(manager, "pi-a")) == 1

    def test_serial_remove_by_legacy_bare_name_refused(self, lab):
        run, manager, _ = lab
        manager.create_serial_device("port-b", "1-1.2")
        manager.db.execute_modify(
            "INSERT INTO serial_ports (sbc_id, port_type, device_path, tcp_port,"
            " baud_rate) VALUES (?, 'console', 'port-b', 4555, 115200)",
            (manager.get_sbc_by_name("pi-a").id,),
        )
        result = run("serial", "remove", "port-b", "--yes")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output

    def test_serial_remove_of_an_unclaimed_boards_adapter(self, lab, monkeypatch):
        run, manager, _ = lab
        manager.create_serial_device("port-c", "1-1.3")
        result = run("serial", "remove", "port-c", "--yes")
        assert result.exit_code == 0, result.output
        assert manager.get_serial_device_by_name("port-c") is None

    def test_serial_remove_refuses_before_asking(self, lab, unlinked, monkeypatch):
        run, manager, _ = lab
        monkeypatch.setattr("labctl.cli._stdin_is_tty", lambda: True)
        result = run("serial", "remove", "port-a", input="y\n")
        assert result.exit_code == 1
        assert "Continue?" not in result.output
        assert manager.get_serial_device_by_name("port-a") is not None

    def test_serial_remove_asks_only_on_a_terminal(self, lab):
        run, manager, _ = lab
        manager.create_serial_device("port-c", "1-1.3")
        result = run("serial", "remove", "port-c")
        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output
        assert manager.get_serial_device_by_name("port-c") is None

    @pytest.fixture
    def rules(self, tmp_path, monkeypatch):
        """The installed rules file, and the rules each install writes."""
        path = tmp_path / "99-lab-serial.rules"
        monkeypatch.setattr("labctl.serial.udev.UDEV_RULES_PATH", path)
        installed = []
        monkeypatch.setattr(
            "labctl.serial.udev.install_udev_rules", lambda text: installed.append(text)
        )
        return path, installed

    def _console_on_link(self, manager, link):
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-a").id, PortType.CONSOLE, f"/dev/lab/{link}"
        )

    def test_udev_install_refused_when_a_claimed_boards_link_moves(self, lab, rules):
        """port-a's adapter was re-registered on another USB path."""
        from labctl.serial.udev import generate_udev_rules

        run, manager, _ = lab
        path, installed = rules
        device = manager.create_serial_device("port-a", "1-1.1")
        path.write_text(generate_udev_rules([device]))
        manager.db.execute_modify(
            "UPDATE serial_devices SET usb_path = '1-9.9' WHERE id = ?", (device.id,)
        )
        self._console_on_link(manager, "port-a")

        result = run("serial", "udev", "--install")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert installed == []

        result = run("serial", "udev", "--install", "--override-claim")
        assert result.exit_code == 0, result.output
        assert len(installed) == 1
        assert len(_overrides(manager, "pi-a")) == 1

    def test_udev_install_refused_when_a_claimed_boards_link_goes(self, lab, rules):
        """port-a was renamed: its old link disappears."""
        from labctl.serial.udev import generate_udev_rules

        run, manager, _ = lab
        path, installed = rules
        device = manager.create_serial_device("port-a", "1-1.1")
        path.write_text(generate_udev_rules([device]))
        manager.db.execute_modify(
            "UPDATE serial_devices SET name = 'port-z' WHERE id = ?", (device.id,)
        )
        manager.db.execute_modify(
            "INSERT INTO serial_ports (sbc_id, port_type, device_path, tcp_port,"
            " baud_rate) VALUES (?, 'console', '/dev/lab/port-a', 4556, 115200)",
            (manager.get_sbc_by_name("pi-a").id,),
        )

        result = run("serial", "udev", "--install")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert installed == []

    def test_udev_install_with_unchanged_links_allowed(self, lab, rules):
        from labctl.serial.udev import generate_udev_rules

        run, manager, _ = lab
        path, installed = rules
        device = manager.create_serial_device("port-a", "1-1.1")
        path.write_text(generate_udev_rules([device]))
        self._console_on_link(manager, "port-a")
        manager.create_serial_device("port-new", "1-1.7")  # only adds a link

        result = run("serial", "udev", "--install")
        assert result.exit_code == 0, result.output
        assert len(installed) == 1

    def test_udev_install_first_time_allowed(self, lab, rules):
        run, manager, _ = lab
        _, installed = rules
        manager.create_serial_device("port-a", "1-1.1")
        self._console_on_link(manager, "port-a")

        result = run("serial", "udev", "--install")
        assert result.exit_code == 0, result.output
        assert len(installed) == 1

    def test_udev_install_unreadable_rules_count_as_every_link(self, lab, rules):
        run, manager, _ = lab
        path, installed = rules
        path.mkdir()  # exists, can't be read as a file
        manager.create_serial_device("port-a", "1-1.1")
        self._console_on_link(manager, "port-a")

        result = run("serial", "udev", "--install")
        assert result.exit_code == 1
        assert "'pi-a' is claimed by 'agent-x'" in result.output
        assert installed == []

    def test_udev_without_install_not_checked(self, lab, rules):
        run, manager, _ = lab
        path, _ = rules
        path.mkdir()
        manager.create_serial_device("port-a", "1-1.1")
        self._console_on_link(manager, "port-a")

        result = run("serial", "udev")
        assert result.exit_code == 0, result.output
        assert 'SYMLINK+="lab/port-a"' in result.output


class TestSudoIdentity:
    """Under sudo the CLI acts for SUDO_USER (#41 review 4)."""

    def test_sudo_user_is_the_identity(self, monkeypatch):
        from labctl import cli

        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_USER", "alice")
        assert cli._cli_user() == "alice"
        assert cli._cli_session_id().startswith("cli-alice@")
        assert cli._default_agent_name() == "alice"

    @pytest.mark.parametrize("euid, sudo_user", [(1000, "alice"), (0, "root"), (0, "")])
    def test_sudo_user_ignored_otherwise(self, monkeypatch, euid, sudo_user):
        """Only a real root process honours SUDO_USER (a user can set it)."""
        import getpass

        from labctl import cli

        monkeypatch.setattr("os.geteuid", lambda: euid)
        monkeypatch.setenv("SUDO_USER", sudo_user)
        monkeypatch.setattr(getpass, "getuser", lambda: "bob")
        assert cli._cli_user() == "bob"

    def test_own_claim_kept_under_sudo(self, lab, tmp_path, monkeypatch):
        """`sudo labctl serial udev --install` doesn't refuse on the
        invoking user's own CLI claim."""
        import getpass

        run, manager, _ = lab
        monkeypatch.setattr(getpass, "getuser", lambda: "alice")
        assert run("claim", "pi-b", "-r", "mine").exit_code == 0

        path = tmp_path / "99-lab-serial.rules"
        device = manager.create_serial_device("port-s", "1-1.1")
        from labctl.serial.udev import generate_udev_rules

        path.write_text(generate_udev_rules([device]))
        manager.db.execute_modify(
            "UPDATE serial_devices SET usb_path = '1-9.9' WHERE id = ?", (device.id,)
        )
        manager.assign_serial_port(
            manager.get_sbc_by_name("pi-b").id, PortType.CONSOLE, "/dev/lab/port-s"
        )
        installed = []
        monkeypatch.setattr("labctl.serial.udev.UDEV_RULES_PATH", path)
        monkeypatch.setattr(
            "labctl.serial.udev.install_udev_rules", lambda t: installed.append(t)
        )

        # Now "sudo": root, with SUDO_USER=alice.
        monkeypatch.setattr(getpass, "getuser", lambda: "root")
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_USER", "alice")
        result = run("serial", "udev", "--install")
        assert result.exit_code == 0, result.output
        assert len(installed) == 1
