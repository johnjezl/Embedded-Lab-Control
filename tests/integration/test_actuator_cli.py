"""Integration tests for actuator + binding CLI commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from labctl.cli import main
from labctl.core.database import Database
from labctl.core.manager import ResourceManager


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def lab(tmp_path):
    """Set up an isolated DB + config and pre-seed an SBC.

    Returns a SimpleNamespace with the config_path, db_path, and the
    seeded sbc record. Tests pass the config via -c so commands hit
    this DB rather than /etc/labctl/config.yaml.
    """
    from types import SimpleNamespace

    db_path = tmp_path / "lab.db"
    db = Database(db_path)
    db.initialize()
    manager = ResourceManager(db)
    sbc = manager.create_sbc(name="jetson-nano-2", project="SLM-OS")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database_path: {db_path}\n")

    return SimpleNamespace(
        config_path=config_path,
        db_path=db_path,
        manager=manager,
        sbc=sbc,
    )


# ---------------------------------------------------------------------------
# `labctl actuator add / list / remove`
# ---------------------------------------------------------------------------


class TestActuatorAdd:
    def test_add_creates_actuator_with_channels(self, runner, lab):
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "add", "relay-1",
                "--driver", "lcus1_serial",
                "--device", "/dev/ttyUSB-relay-1",
                "--channels", "1",
            ],
        )
        assert result.exit_code == 0, result.output

        a = lab.manager.get_actuator_by_name("relay-1")
        assert a is not None
        assert a.driver.value == "lcus1_serial"
        assert a.device_path == "/dev/ttyUSB-relay-1"
        assert len(a.channels) == 1
        assert a.channels[0].channel_index == 1

    def test_add_unsupported_driver_errors(self, runner, lab):
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "add", "relay-x",
                "--driver", "numato_acm",
                "--channels", "1",
            ],
        )
        assert result.exit_code != 0
        assert "not implemented" in result.output.lower()

    def test_add_zero_channels_rejected(self, runner, lab):
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "add", "relay-x",
                "--driver", "lcus1_serial",
                "--channels", "0",
            ],
        )
        assert result.exit_code != 0
        assert "at least 1" in result.output


class TestActuatorListAndRemove:
    def test_list_shows_provisioned(self, runner, lab):
        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "add", "relay-a",
                "--driver", "lcus1_serial",
                "--channels", "1",
            ],
        )
        result = runner.invoke(
            main, ["-c", str(lab.config_path), "actuator", "list"]
        )
        assert result.exit_code == 0, result.output
        assert "relay-a" in result.output
        assert "lcus1_serial" in result.output

    def test_remove_with_yes(self, runner, lab):
        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "add", "relay-b",
                "--driver", "lcus1_serial",
                "--channels", "1",
            ],
        )
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "remove", "relay-b", "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert lab.manager.get_actuator_by_name("relay-b") is None


# ---------------------------------------------------------------------------
# `labctl bind`
# ---------------------------------------------------------------------------


def _add_relay(runner, config_path, name="relay-1", channels=1):
    return runner.invoke(
        main,
        [
            "-c", str(config_path),
            "actuator", "add", name,
            "--driver", "lcus1_serial",
            "--device", f"/dev/ttyUSB-{name}",
            "--channels", str(channels),
        ],
    )


class TestBindCommand:
    def test_bind_latch_success(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch",
                "--active-when", "closed",
                "--phase", "pre-power",
            ],
        )
        assert result.exit_code == 0, result.output

        b = lab.manager.get_binding_by_target(lab.sbc.id, "recovery_mode")
        assert b is not None
        assert b.shape_mode.value == "latch"
        assert b.shape_active.value == "closed"
        assert b.sample_phase.value == "pre_power"
        assert b.desired_state.value == "released"  # default

    def test_bind_momentary_requires_pulse_ms(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "power_button",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "momentary",
                "--active-when", "closed",
            ],
        )
        assert result.exit_code != 0
        assert "--pulse-ms" in result.output

    def test_bind_latch_rejects_pulse_ms(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch",
                "--active-when", "closed",
                "--pulse-ms", "200",
            ],
        )
        assert result.exit_code != 0
        assert "only applies to momentary" in result.output

    def test_bind_rejects_default_state_equal_to_active(self, runner, lab):
        """Wiring polarity check: default==active means always-asserted."""
        _add_relay(runner, lab.config_path)
        # Channels are added with default_state=open, so binding active=open
        # would mean the device always sees the strap engaged.
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch",
                "--active-when", "open",
            ],
        )
        assert result.exit_code != 0
        assert "always be" in result.output
        assert "asserted" in result.output

    def test_bind_unknown_sbc(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "ghost", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch",
                "--active-when", "closed",
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_bind_unknown_channel(self, runner, lab):
        _add_relay(runner, lab.config_path, channels=1)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "99",
                "--mode", "latch",
                "--active-when", "closed",
            ],
        )
        assert result.exit_code != 0
        assert "not on" in result.output


class TestUnbindAndList:
    def test_unbind_removes_binding(self, runner, lab):
        _add_relay(runner, lab.config_path)
        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch", "--active-when", "closed",
            ],
        )
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "unbind", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 0, result.output
        assert lab.manager.get_binding_by_target(
            lab.sbc.id, "recovery_mode"
        ) is None

    def test_bindings_list_filters_by_target(self, runner, lab):
        _add_relay(runner, lab.config_path, channels=2)
        # Bind one purpose on jetson, another on a fresh SBC.
        lab.manager.create_sbc(name="pi-5-1")
        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch", "--active-when", "closed",
            ],
        )
        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "pi-5-1", "power_button",
                "--actuator", "relay-1", "--channel", "2",
                "--mode", "momentary", "--active-when", "closed",
                "--pulse-ms", "200",
            ],
        )

        all_b = runner.invoke(
            main, ["-c", str(lab.config_path), "bindings", "list"]
        )
        assert "jetson-nano-2" in all_b.output
        assert "pi-5-1" in all_b.output

        only = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "list", "--target", "jetson-nano-2",
            ],
        )
        assert "jetson-nano-2" in only.output
        assert "pi-5-1" not in only.output


# ---------------------------------------------------------------------------
# `labctl actuator probe / set`
# ---------------------------------------------------------------------------


class _StubDriver:
    """Test double standing in for ``get_driver``'s return value.

    Records open/close/set/probe interactions so tests can assert
    behaviour without needing pyserial.
    """

    def __init__(self):
        from labctl.actuators.base import ProbeOutcome, ProbeResult, WriteOutcome

        self.opened = False
        self.closed = False
        self.set_calls: list[tuple[int, bool]] = []
        self.write_outcome = WriteOutcome.OK
        self.probe_outcome = ProbeOutcome(result=ProbeResult.OK)

    def open(self, transport):
        self.opened = True

    def close(self):
        self.closed = True

    def set_channel(self, index: int, *, closed: bool):
        self.set_calls.append((index, closed))
        return self.write_outcome

    def get_channel(self, index: int):
        return None

    def channel_count(self):
        return 1

    def probe(self):
        return self.probe_outcome


class TestActuatorProbe:
    def test_probe_records_result(self, runner, lab, monkeypatch):
        from labctl.actuators.base import ProbeOutcome, ProbeResult

        _add_relay(runner, lab.config_path)
        stub = _StubDriver()
        stub.probe_outcome = ProbeOutcome(result=ProbeResult.OK)
        monkeypatch.setattr("labctl.actuators.get_driver", lambda *a, **kw: stub)

        result = runner.invoke(
            main,
            ["-c", str(lab.config_path), "actuator", "probe", "relay-1"],
        )
        assert result.exit_code == 0, result.output
        assert "Probe: ok" in result.output
        assert stub.opened and stub.closed

        a = lab.manager.get_actuator_by_name("relay-1")
        assert a.last_probe_result == "ok"
        assert a.last_probe_at is not None

    def test_probe_unreachable_exits_nonzero(self, runner, lab, monkeypatch):
        from labctl.actuators.base import ProbeOutcome, ProbeResult

        _add_relay(runner, lab.config_path)
        stub = _StubDriver()
        stub.probe_outcome = ProbeOutcome(
            result=ProbeResult.UNREACHABLE, detail="port missing"
        )
        monkeypatch.setattr("labctl.actuators.get_driver", lambda *a, **kw: stub)

        result = runner.invoke(
            main,
            ["-c", str(lab.config_path), "actuator", "probe", "relay-1"],
        )
        assert result.exit_code != 0
        assert "unreachable" in result.output
        assert "port missing" in result.output

        a = lab.manager.get_actuator_by_name("relay-1")
        assert a.last_probe_result == "unreachable"


class TestVerbs:
    """End-to-end CLI tests for actuate/release/press/bindings status."""

    def _bind_latch(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch",
                "--active-when", "closed",
                "--phase", "pre-power",
            ],
        )
        assert result.exit_code == 0, result.output

    def _bind_momentary(self, runner, lab):
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "power_button",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "momentary",
                "--active-when", "closed",
                "--pulse-ms", "100",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_actuate_writes_through_runtime(self, runner, lab, monkeypatch):
        from labctl.actuators.base import WriteOutcome
        from labctl.actuators.mock import MockRelayDriver

        self._bind_latch(runner, lab)
        mock = MockRelayDriver(channel_count=1)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: mock,
        )

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "actuate", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 0, result.output
        assert mock.write_log == [(1, True, WriteOutcome.OK)]

        b = lab.manager.get_binding_by_target(lab.sbc.id, "recovery_mode")
        assert b.desired_state.value == "asserted"

    def test_release_after_actuate(self, runner, lab, monkeypatch):
        from labctl.actuators.mock import MockRelayDriver

        self._bind_latch(runner, lab)
        mock = MockRelayDriver(channel_count=1)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: mock,
        )

        runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "actuate", "jetson-nano-2", "recovery_mode",
            ],
        )
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "release", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 0, result.output

        b = lab.manager.get_binding_by_target(lab.sbc.id, "recovery_mode")
        assert b.desired_state.value == "released"

    def test_actuate_on_momentary_binding_errors(self, runner, lab, monkeypatch):
        from labctl.actuators.mock import MockRelayDriver

        self._bind_momentary(runner, lab)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: MockRelayDriver(channel_count=1),
        )

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "actuate", "jetson-nano-2", "power_button",
            ],
        )
        assert result.exit_code == 2
        assert "latch" in result.output

    def test_press_on_latch_binding_errors(self, runner, lab, monkeypatch):
        from labctl.actuators.mock import MockRelayDriver

        self._bind_latch(runner, lab)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: MockRelayDriver(channel_count=1),
        )

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "press", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 2
        assert "momentary" in result.output

    def test_press_drives_pulse(self, runner, lab, monkeypatch):
        from labctl.actuators.mock import MockRelayDriver

        self._bind_momentary(runner, lab)
        mock = MockRelayDriver(channel_count=1)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: mock,
        )
        # Skip actual sleep so the test runs fast.
        monkeypatch.setattr("labctl.actuators.runtime.time.sleep", lambda s: None)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "press", "jetson-nano-2", "power_button",
            ],
        )
        assert result.exit_code == 0, result.output

        # Two writes: assert then release.
        states = [(idx, closed) for (idx, closed, _) in mock.write_log]
        assert states == [(1, True), (1, False)]

    def test_actuation_error_exits_one(self, runner, lab, monkeypatch):
        from labctl.actuators.base import WriteOutcome
        from labctl.actuators.mock import MockRelayDriver

        self._bind_latch(runner, lab)
        mock = MockRelayDriver(channel_count=1)
        mock.next_write_outcome = WriteOutcome.DEVICE_GONE
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: mock,
        )

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "actuate", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 1
        assert "device_gone" in result.output

    def test_bindings_status_renders_state(self, runner, lab):
        self._bind_latch(runner, lab)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bindings", "status", "jetson-nano-2", "recovery_mode",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "jetson-nano-2:recovery_mode" in result.output
        assert "relay-1[1]" in result.output
        assert "Desired:" in result.output
        # No actuation yet → desired stays at the default.
        assert "released" in result.output


class TestEnterExitRecovery:
    def _setup(self, runner, lab, monkeypatch, controller_calls):
        from labctl.actuators.mock import MockRelayDriver
        from labctl.core.models import PlugType

        # SBC needs a power plug for the composite to construct a controller.
        lab.manager.assign_power_plug(
            lab.sbc.id, PlugType.TASMOTA, address="10.0.0.1"
        )

        # Pre-bind recovery_mode (latch, pre-power).
        _add_relay(runner, lab.config_path)
        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "bind", "jetson-nano-2", "recovery_mode",
                "--actuator", "relay-1", "--channel", "1",
                "--mode", "latch", "--active-when", "closed",
                "--phase", "pre-power",
            ],
        )
        assert result.exit_code == 0, result.output

        # Stub the driver and the power controller.
        mock_driver = MockRelayDriver(channel_count=1)
        monkeypatch.setattr(
            "labctl.actuators.runtime.get_driver",
            lambda *a, **kw: mock_driver,
        )

        class FakeController:
            def power_off(self):
                controller_calls.append("off")
                return True

            def power_on(self):
                controller_calls.append("on")
                return True

        monkeypatch.setattr(
            "labctl.cli._get_power_controller",
            lambda manager, sbc_name: (FakeController(), lab.sbc),
        )
        # Skip real sleeps.
        monkeypatch.setattr("labctl.actuators.runtime.time.sleep", lambda s: None)
        monkeypatch.setattr("labctl.cli.time.sleep", lambda s: None)

        return mock_driver

    def test_enter_recovery_sequence(self, runner, lab, monkeypatch):
        calls: list[str] = []
        mock = self._setup(runner, lab, monkeypatch, calls)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "enter-recovery", "jetson-nano-2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == ["off", "on"]
        # Strap engaged twice (actuate + apply_pre_power_bindings).
        states = [(idx, closed) for (idx, closed, _) in mock.write_log]
        assert states == [(1, True), (1, True)]

        b = lab.manager.get_binding_by_target(lab.sbc.id, "recovery_mode")
        assert b.desired_state.value == "asserted"

    def test_exit_recovery_sequence(self, runner, lab, monkeypatch):
        calls: list[str] = []
        mock = self._setup(runner, lab, monkeypatch, calls)

        # Pretend we're in recovery already.
        b = lab.manager.get_binding_by_target(lab.sbc.id, "recovery_mode")
        from labctl.core.models import DesiredState

        lab.manager.update_binding_desired_state(b.id, DesiredState.ASSERTED)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "exit-recovery", "jetson-nano-2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == ["off", "on"]
        # Strap released twice (release + apply_pre_power_bindings).
        states = [(idx, closed) for (idx, closed, _) in mock.write_log]
        assert states == [(1, False), (1, False)]

        fresh = lab.manager.get_binding(b.id)
        assert fresh.desired_state.value == "released"

    def test_enter_recovery_without_binding_errors(self, runner, lab, monkeypatch):
        from labctl.core.models import PlugType

        lab.manager.assign_power_plug(
            lab.sbc.id, PlugType.TASMOTA, address="10.0.0.1"
        )
        # No bind invocation — composite must refuse.
        monkeypatch.setattr(
            "labctl.cli._get_power_controller",
            lambda manager, sbc_name: (object(), lab.sbc),
        )

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "enter-recovery", "jetson-nano-2",
            ],
        )
        assert result.exit_code == 1
        assert "no 'recovery_mode'" in result.output


class TestActuatorSet:
    def test_set_drives_channel_and_updates_state(self, runner, lab, monkeypatch):
        _add_relay(runner, lab.config_path)
        stub = _StubDriver()
        monkeypatch.setattr("labctl.actuators.get_driver", lambda *a, **kw: stub)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "set", "relay-1", "1", "closed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert stub.set_calls == [(1, True)]

        a = lab.manager.get_actuator_by_name("relay-1")
        assert a.channels[0].last_state.value == "closed"
        assert a.channels[0].cycle_count == 1

    def test_set_propagates_write_failure(self, runner, lab, monkeypatch):
        from labctl.actuators.base import WriteOutcome

        _add_relay(runner, lab.config_path)
        stub = _StubDriver()
        stub.write_outcome = WriteOutcome.DEVICE_GONE
        monkeypatch.setattr("labctl.actuators.get_driver", lambda *a, **kw: stub)

        result = runner.invoke(
            main,
            [
                "-c", str(lab.config_path),
                "actuator", "set", "relay-1", "1", "closed",
            ],
        )
        assert result.exit_code != 0
        assert "device_gone" in result.output

        # Channel state must not be persisted on failure.
        a = lab.manager.get_actuator_by_name("relay-1")
        assert a.channels[0].last_state is None
        assert a.channels[0].cycle_count == 0
