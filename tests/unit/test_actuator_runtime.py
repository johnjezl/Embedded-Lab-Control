"""Tests for the actuator runtime layer (actuate/release/press/status)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from labctl.actuators import runtime
from labctl.actuators.base import ProbeResult, WriteOutcome
from labctl.actuators.mock import MockRelayDriver
from labctl.core.database import Database
from labctl.core.manager import ResourceManager
from labctl.core.models import (
    ChannelState,
    DesiredState,
    DriverName,
    SamplePhase,
    ShapeMode,
)


@pytest.fixture(autouse=True)
def _clear_runtime():
    runtime._clear_runtime_state()
    yield
    runtime._clear_runtime_state()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Set up DB + a driver patch so all runtime ops use a shared mock."""
    db = Database(tmp_path / "rt.db")
    db.initialize()
    manager = ResourceManager(db)
    sbc = manager.create_sbc(name="jetson-nano-2")
    actuator = manager.create_actuator(
        "relay-1",
        DriverName.LCUS1_SERIAL,
        device_path="/dev/ttyUSB-relay-1",
    )
    channel = manager.add_actuator_channel(
        actuator.id, 1, default_state=ChannelState.OPEN
    )

    mock_driver = MockRelayDriver(channel_count=1)
    monkeypatch.setattr(
        "labctl.actuators.runtime.get_driver",
        lambda *a, **kw: mock_driver,
    )

    return type(
        "Lab",
        (),
        {
            "manager": manager,
            "sbc": sbc,
            "actuator": actuator,
            "channel": channel,
            "driver": mock_driver,
        },
    )


def _make_binding(
    lab,
    *,
    purpose="recovery_mode",
    shape_mode=ShapeMode.LATCH,
    shape_active=ChannelState.CLOSED,
    momentary_pulse_ms=None,
    sample_phase=SamplePhase.PRE_POWER,
):
    return lab.manager.create_binding(
        lab.sbc.id,
        purpose,
        lab.channel.id,
        shape_mode=shape_mode,
        shape_active=shape_active,
        momentary_pulse_ms=momentary_pulse_ms,
        sample_phase=sample_phase,
    )


# ---------------------------------------------------------------------------
# Latch verbs (actuate / release)
# ---------------------------------------------------------------------------


class TestActuateRelease:
    def test_actuate_drives_to_active_and_persists_intent(self, lab):
        binding = _make_binding(lab)
        runtime.actuate_binding(lab.manager, binding)

        assert lab.driver.write_log[-1] == (1, True, WriteOutcome.OK)

        fresh = lab.manager.get_binding(binding.id)
        assert fresh.desired_state is DesiredState.ASSERTED

        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        assert ch.last_state is ChannelState.CLOSED
        assert ch.cycle_count == 1

    def test_release_drives_opposite_and_persists_intent(self, lab):
        binding = _make_binding(lab)
        runtime.actuate_binding(lab.manager, binding)
        runtime.release_binding(lab.manager, binding)

        assert lab.driver.write_log[-1] == (1, False, WriteOutcome.OK)

        fresh = lab.manager.get_binding(binding.id)
        assert fresh.desired_state is DesiredState.RELEASED

    def test_actuate_with_open_active(self, lab):
        """Wiring polarity: when active=open, actuate drives the channel open."""
        # Channel default is open; bindings forbid default==active normally,
        # but the runtime itself just trusts shape_active. We seed a binding
        # with active=open by first changing the channel's default to closed.
        lab.manager.db.execute_modify(
            "UPDATE actuator_channels SET default_state = 'closed' WHERE id = ?",
            (lab.channel.id,),
        )
        binding = _make_binding(lab, shape_active=ChannelState.OPEN)
        runtime.actuate_binding(lab.manager, binding)
        assert lab.driver.write_log[-1] == (1, False, WriteOutcome.OK)

    def test_actuate_on_momentary_binding_raises_shape_error(self, lab):
        binding = _make_binding(
            lab,
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=200,
        )
        with pytest.raises(runtime.BindingShapeError, match="actuate"):
            runtime.actuate_binding(lab.manager, binding)

    def test_release_on_momentary_binding_raises_shape_error(self, lab):
        binding = _make_binding(
            lab,
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=200,
        )
        with pytest.raises(runtime.BindingShapeError, match="release"):
            runtime.release_binding(lab.manager, binding)


class TestActuateFailureSemantics:
    def test_write_failure_does_not_persist_state(self, lab):
        binding = _make_binding(lab)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE

        with pytest.raises(runtime.ActuationError, match="device_gone"):
            runtime.actuate_binding(lab.manager, binding)

        # last_state and desired_state must remain untouched.
        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        assert ch.last_state is None
        assert ch.cycle_count == 0
        fresh = lab.manager.get_binding(binding.id)
        assert fresh.desired_state is DesiredState.RELEASED  # default

    def test_lock_released_after_failure(self, lab):
        """A failed actuate must release the channel lock so the next op runs."""
        binding = _make_binding(lab)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE

        with pytest.raises(runtime.ActuationError):
            runtime.actuate_binding(lab.manager, binding)

        # Without lock release, this would raise ChannelBusyError.
        runtime.actuate_binding(lab.manager, binding)


# ---------------------------------------------------------------------------
# Press verb
# ---------------------------------------------------------------------------


class TestPress:
    def test_press_drives_active_then_inactive(self, lab):
        binding = _make_binding(
            lab,
            purpose="power_button",
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=200,
            sample_phase=SamplePhase.NONE,
        )

        slept_for: list[float] = []
        runtime.press_binding(
            lab.manager,
            binding,
            sleep_fn=lambda s: slept_for.append(s),
        )

        # Two writes: assert (close) then release (open).
        states = [(idx, closed) for (idx, closed, _outcome) in lab.driver.write_log]
        assert states == [(1, True), (1, False)]
        assert slept_for == [0.2]

        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        assert ch.last_state is ChannelState.OPEN  # ended in inactive state
        assert ch.cycle_count == 2  # both writes counted

    def test_press_on_latch_binding_raises_shape_error(self, lab):
        binding = _make_binding(lab)  # latch
        with pytest.raises(runtime.BindingShapeError, match="press"):
            runtime.press_binding(lab.manager, binding, sleep_fn=lambda s: None)

    def test_press_without_pulse_ms_raises(self, lab):
        binding = _make_binding(
            lab, shape_mode=ShapeMode.MOMENTARY, momentary_pulse_ms=None
        )
        with pytest.raises(runtime.ActuationError, match="momentary_pulse_ms"):
            runtime.press_binding(lab.manager, binding, sleep_fn=lambda s: None)

    def test_press_first_write_failure_aborts_before_sleep(self, lab):
        binding = _make_binding(
            lab,
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=200,
        )
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE

        slept = []
        with pytest.raises(runtime.ActuationError, match="failed to assert"):
            runtime.press_binding(
                lab.manager, binding, sleep_fn=lambda s: slept.append(s)
            )

        # Sleep must NOT have happened — assert failed before timing.
        assert slept == []
        # Channel state untouched.
        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        assert ch.last_state is None

    def test_press_second_write_failure_warns_and_persists_partial(self, lab):
        """If assert succeeds but release fails, surface the error.

        last_state reflects the last successful drive (asserted), so the
        operator can see the channel is left in an unexpected state.
        """
        binding = _make_binding(
            lab,
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=50,
        )

        # Set up: first write OK, then second write fails.
        original_set = lab.driver.set_channel
        call_count = [0]

        def flaky_set(index, *, closed):
            call_count[0] += 1
            if call_count[0] == 2:
                # Inject failure on the release-side write.
                lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE
            return original_set(index, closed=closed)

        lab.driver.set_channel = flaky_set

        with pytest.raises(runtime.ActuationError, match="release failed"):
            runtime.press_binding(lab.manager, binding, sleep_fn=lambda s: None)

        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        # First write succeeded → last_state was stamped to active (closed).
        assert ch.last_state is ChannelState.CLOSED
        assert ch.cycle_count == 1


# ---------------------------------------------------------------------------
# Per-channel locks
# ---------------------------------------------------------------------------


class TestChannelLock:
    def test_concurrent_actuate_one_succeeds_one_busy(self, lab):
        binding = _make_binding(lab)

        in_lock = threading.Event()
        gate = threading.Event()
        original_set = lab.driver.set_channel

        def slow_set(index, *, closed):
            in_lock.set()
            # Hold the channel lock until the main thread releases gate.
            gate.wait(timeout=2.0)
            return original_set(index, closed=closed)

        lab.driver.set_channel = slow_set

        results: list = [None, None]

        def worker(i):
            try:
                runtime.actuate_binding(lab.manager, binding)
                results[i] = "ok"
            except runtime.ChannelBusyError:
                results[i] = "busy"

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        # Wait until t1 has entered slow_set (i.e. it holds the channel lock).
        assert in_lock.wait(timeout=2.0), "t1 did not enter slow_set"
        t2.start()
        t2.join()  # t2 returns immediately with ChannelBusyError
        gate.set()
        t1.join()

        # t1 must have succeeded; t2 must have hit the busy error.
        assert results[0] == "ok"
        assert results[1] == "busy"


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------


class TestBindingStatus:
    def test_status_includes_persistent_fields(self, lab):
        binding = _make_binding(lab)
        info = runtime.binding_status(lab.manager, binding)
        assert info["binding"]["sbc"] == "jetson-nano-2"
        assert info["binding"]["purpose"] == "recovery_mode"
        assert info["binding"]["desired_state"] == "released"
        assert info["actuator"]["name"] == "relay-1"
        assert info["channel"]["index"] == 1
        assert info["channel"]["cycle_count"] == 0


class TestApplyPrePowerBindings:
    def test_drives_active_when_desired_asserted(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert lab.driver.write_log[-1] == (1, True, WriteOutcome.OK)

    def test_drives_inactive_when_desired_released(self, lab):
        _make_binding(lab)
        # Default desired_state is RELEASED.
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert lab.driver.write_log[-1] == (1, False, WriteOutcome.OK)

    def test_skips_following_power(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(
            binding.id, DesiredState.FOLLOWING_POWER
        )
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert lab.driver.write_log == []

    def test_skips_non_pre_power_bindings(self, lab):
        _make_binding(lab, sample_phase=SamplePhase.NONE)
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert lab.driver.write_log == []

    def test_no_bindings_is_a_no_op(self, lab):
        # Don't make any bindings at all.
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert lab.driver.write_log == []

    def test_write_failure_raises_actuation_error(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE
        with pytest.raises(runtime.ActuationError, match="device_gone"):
            runtime.apply_pre_power_bindings(lab.manager, lab.sbc)


class TestDriverOpenFailure:
    """A device the driver can't open (e.g. permission denied) is an
    ActuationError, which every verb's caller reports, not a bare
    DriverError that escapes as a traceback."""

    def test_pre_power_open_failure_is_actuation_error(self, lab, monkeypatch):
        from labctl.actuators.base import DriverError

        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        def refuse(transport):
            raise DriverError("permission denied opening /dev/x: join dialout")

        monkeypatch.setattr(lab.driver, "open", refuse)
        with pytest.raises(runtime.ActuationError) as exc:
            runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        assert str(exc.value) == "permission denied opening /dev/x: join dialout"
        assert isinstance(exc.value.__cause__, DriverError)
        assert lab.driver.write_log == []

    def test_actuate_open_failure_is_actuation_error(self, lab, monkeypatch):
        from labctl.actuators.base import DriverError

        binding = _make_binding(lab)

        def refuse(transport):
            raise DriverError("failed to open /dev/x: gone")

        monkeypatch.setattr(lab.driver, "open", refuse)
        with pytest.raises(runtime.ActuationError, match="failed to open"):
            runtime.actuate_binding(lab.manager, binding)

    def test_unimplemented_driver_is_actuation_error(self, lab, monkeypatch):
        binding = _make_binding(lab)

        def unimplemented(*a, **kw):
            raise NotImplementedError("driver 'numato_acm' is not implemented")

        monkeypatch.setattr("labctl.actuators.runtime.get_driver", unimplemented)
        with pytest.raises(runtime.ActuationError, match="not implemented"):
            runtime.actuate_binding(lab.manager, binding)

    def test_probe_detail_names_the_actuator_once(self, lab, monkeypatch):
        from labctl.actuators.base import DriverError

        def refuse(transport):
            raise DriverError("permission denied opening /dev/x: hint")

        monkeypatch.setattr(lab.driver, "open", refuse)
        outcome = runtime.probe_actuator(lab.actuator)
        assert outcome.detail == "permission denied opening /dev/x: hint"


class TestCheckPrePowerActuators:
    """power cycle checks the relays before powering off (read-only)."""

    def test_unopenable_relay_raises_before_anything_is_driven(self, lab, monkeypatch):
        from labctl.actuators.base import DriverError

        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        def refuse(transport):
            raise DriverError("permission denied opening /dev/x: hint")

        monkeypatch.setattr(lab.driver, "open", refuse)
        with pytest.raises(runtime.PrePowerCheckError) as exc:
            runtime.check_pre_power_actuators(lab.manager, lab.sbc)
        assert str(exc.value) == (
            "actuator 'relay-1' for 'recovery_mode' can't be used: "
            "permission denied opening /dev/x: hint"
        )
        assert lab.driver.write_log == []

    def test_reachable_relay_passes_without_writing(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        runtime.check_pre_power_actuators(lab.manager, lab.sbc)
        assert lab.driver.write_log == []

    def test_released_strap_is_checked_too(self, lab, monkeypatch):
        _make_binding(lab)  # desired_state defaults to RELEASED
        probed = []
        monkeypatch.setattr(
            runtime,
            "probe_actuator",
            lambda a: probed.append(a.name)
            or runtime.ProbeOutcome(result=ProbeResult.OK),
        )
        runtime.check_pre_power_actuators(lab.manager, lab.sbc)
        assert probed == ["relay-1"]

    def test_broken_binding_fails_the_check(self, lab, monkeypatch):
        """apply_pre_power_bindings resolves every pre_power binding, so a
        binding to a missing channel must fail before power-off too, even
        one it wouldn't drive."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(
            binding.id, DesiredState.FOLLOWING_POWER
        )

        def missing(manager, b):
            raise runtime.ActuationError("references missing channel")

        monkeypatch.setattr(runtime, "_resolve", missing)
        with pytest.raises(runtime.PrePowerCheckError, match="missing channel"):
            runtime.check_pre_power_actuators(lab.manager, lab.sbc)

    def test_each_actuator_probed_once(self, lab, monkeypatch):
        """Two bindings on one relay probe it once; a second relay is
        still probed."""
        _make_binding(lab, purpose="recovery_mode")
        ch2 = lab.manager.add_actuator_channel(
            lab.actuator.id, 2, default_state=ChannelState.OPEN
        )
        lab.manager.create_binding(
            lab.sbc.id,
            "boot_select",
            ch2.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
            sample_phase=SamplePhase.PRE_POWER,
        )
        other = lab.manager.create_actuator(
            "relay-2", DriverName.LCUS1_SERIAL, device_path="/dev/ttyUSB-relay-2"
        )
        other_ch = lab.manager.add_actuator_channel(
            other.id, 1, default_state=ChannelState.OPEN
        )
        lab.manager.create_binding(
            lab.sbc.id,
            "power_button",
            other_ch.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
            sample_phase=SamplePhase.PRE_POWER,
        )
        probed = []
        monkeypatch.setattr(
            runtime,
            "probe_actuator",
            lambda a: probed.append(a.name)
            or runtime.ProbeOutcome(result=ProbeResult.OK),
        )
        runtime.check_pre_power_actuators(lab.manager, lab.sbc)
        assert sorted(probed) == ["relay-1", "relay-2"]

    @pytest.mark.parametrize(
        "phase,desired",
        [
            (SamplePhase.NONE, DesiredState.ASSERTED),
            (SamplePhase.PRE_POWER, DesiredState.FOLLOWING_POWER),
        ],
    )
    def test_skips_bindings_apply_wont_drive(self, lab, monkeypatch, phase, desired):
        binding = _make_binding(lab, sample_phase=phase)
        lab.manager.update_binding_desired_state(binding.id, desired)
        monkeypatch.setattr(runtime, "probe_actuator", lambda a: pytest.fail("probed"))
        runtime.check_pre_power_actuators(lab.manager, lab.sbc)


class TestEnterRecovery:
    def _make_binding_and_controller(self, lab):
        binding = _make_binding(lab, purpose="recovery_mode")

        class FakeController:
            def __init__(self):
                self.calls: list[str] = []

            def power_off(self):
                self.calls.append("off")
                return True

            def power_on(self):
                self.calls.append("on")
                return True

        return binding, FakeController()

    def test_full_sequence(self, lab):
        binding, controller = self._make_binding_and_controller(lab)

        slept: list[float] = []
        runtime.enter_recovery(
            lab.manager,
            lab.sbc,
            controller,
            delay_s=2.0,
            sleep_fn=lambda s: slept.append(s),
        )

        # Sequence: off → sleep → assert → on. The strap is engaged
        # twice: once by actuate_binding (step 3) and once by
        # apply_pre_power_bindings (step 4); both write True.
        assert controller.calls == ["off", "on"]
        assert slept == [2.0]
        states = [(idx, closed) for (idx, closed, _) in lab.driver.write_log]
        # First write: actuate_binding drives to active=closed.
        # Second write: pre_power binding application also drives closed.
        assert states == [(1, True), (1, True)]

        # Persistent intent reflects the actuation.
        fresh = lab.manager.get_binding(binding.id)
        assert fresh.desired_state is DesiredState.ASSERTED

    def test_pre_flight_probe_failure_aborts_before_power_off(self, lab, monkeypatch):
        binding, controller = self._make_binding_and_controller(lab)
        from labctl.actuators.base import ProbeOutcome, ProbeResult

        monkeypatch.setattr(
            "labctl.actuators.runtime.probe_actuator",
            lambda actuator: ProbeOutcome(
                result=ProbeResult.UNREACHABLE, detail="usb yanked"
            ),
        )
        with pytest.raises(runtime.ActuationError, match="probe failed"):
            runtime.enter_recovery(
                lab.manager,
                lab.sbc,
                controller,
                delay_s=2.0,
                sleep_fn=lambda s: None,
            )
        # Crucially, no power transitions occurred.
        assert controller.calls == []
        assert lab.driver.write_log == []

    def test_enter_recovery_does_not_double_count_cycles(self, lab):
        """The trailing apply_pre_power_bindings re-asserts an already-
        engaged strap; cycle_count should reflect ONE physical transition
        per enter_recovery call, not two."""
        binding, controller = self._make_binding_and_controller(lab)

        runtime.enter_recovery(
            lab.manager,
            lab.sbc,
            controller,
            delay_s=0.0,
            sleep_fn=lambda s: None,
        )

        ch = lab.manager.get_actuator_channel(lab.actuator.id, 1)
        # Two driver writes happen but both target the same state →
        # only the first counts toward wear.
        assert ch.cycle_count == 1
        assert ch.last_state is ChannelState.CLOSED

    def test_no_recovery_binding_raises(self, lab):
        controller = type(
            "Stub",
            (),
            {"power_off": lambda self: True, "power_on": lambda self: True},
        )()
        with pytest.raises(runtime.ActuationError, match="no 'recovery_mode'"):
            runtime.enter_recovery(
                lab.manager,
                lab.sbc,
                controller,
                delay_s=1.0,
                sleep_fn=lambda s: None,
            )


class TestRecoveryChecksOtherPrePowerRelays:
    """enter/exit_recovery's power-on drives every pre_power binding, so
    a second relay that can't be opened stops them before power-off."""

    @pytest.mark.parametrize("composite", ["enter_recovery", "exit_recovery"])
    def test_refuses_before_power_off(self, lab, monkeypatch, composite):
        _make_binding(lab)
        calls = []
        monkeypatch.setattr(
            runtime,
            "check_pre_power_actuators",
            lambda m, s: (_ for _ in ()).throw(runtime.ActuationError("relay-2")),
        )

        class Controller:
            def power_off(self):
                calls.append("off")
                return True

            def power_on(self):
                calls.append("on")
                return True

        with pytest.raises(runtime.ActuationError, match="relay-2"):
            getattr(runtime, composite)(
                lab.manager, lab.sbc, Controller(), delay_s=0, sleep_fn=lambda s: None
            )
        assert calls == []


class TestExitRecovery:
    def test_full_sequence(self, lab):
        binding = _make_binding(lab, purpose="recovery_mode")
        # Pretend we entered recovery.
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        class FakeController:
            def __init__(self):
                self.calls = []

            def power_off(self):
                self.calls.append("off")
                return True

            def power_on(self):
                self.calls.append("on")
                return True

        controller = FakeController()
        runtime.exit_recovery(
            lab.manager,
            lab.sbc,
            controller,
            delay_s=2.0,
            sleep_fn=lambda s: None,
        )

        assert controller.calls == ["off", "on"]
        # release_binding drives inactive (open), then apply_pre_power_bindings
        # also drives inactive (since desired is now RELEASED).
        states = [(idx, closed) for (idx, closed, _) in lab.driver.write_log]
        assert states == [(1, False), (1, False)]

        fresh = lab.manager.get_binding(binding.id)
        assert fresh.desired_state is DesiredState.RELEASED


class TestLockDictPruning:
    def test_drop_channel_lock_removes_entry(self, lab):
        runtime._get_channel_lock(lab.channel.id)
        assert lab.channel.id in runtime._channel_locks
        runtime.drop_channel_lock(lab.channel.id)
        assert lab.channel.id not in runtime._channel_locks
        # Calling again is a no-op.
        runtime.drop_channel_lock(lab.channel.id)

    def test_delete_actuator_drops_locks(self, lab):
        # Take a lock to seed the dict, then delete and confirm it's gone.
        runtime._get_channel_lock(lab.channel.id)
        assert lab.channel.id in runtime._channel_locks

        lab.manager.delete_actuator(lab.actuator.id)
        assert lab.channel.id not in runtime._channel_locks


class TestApplySafeDriveOnStartup:
    def test_drives_unbound_channels_to_default(self, lab):
        # Add a second channel with no binding.
        lab.manager.add_actuator_channel(
            lab.actuator.id, 2, default_state=ChannelState.OPEN
        )
        lab.driver._configured_count = 2  # mock can talk to ch 2

        result = runtime.apply_safe_drive_on_startup(lab.manager)

        # Both channels driven to default (open).
        states = [(idx, closed) for (idx, closed, _) in lab.driver.write_log]
        assert (1, False) in states
        assert (2, False) in states
        assert len(result["drove"]) == 2
        assert result["held"] == []

    def test_held_asserted_binding_left_alone(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        result = runtime.apply_safe_drive_on_startup(lab.manager)

        # The held channel must NOT be written.
        states = [(idx, closed) for (idx, closed, _) in lab.driver.write_log]
        assert (1, False) not in states
        assert len(result["held"]) == 1
        assert result["held"][0]["purpose"] == "recovery_mode"
        assert result["held"][0]["desired_state"] == "asserted"

    def test_following_power_left_alone(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(
            binding.id, DesiredState.FOLLOWING_POWER
        )

        result = runtime.apply_safe_drive_on_startup(lab.manager)
        assert lab.driver.write_log == []
        assert len(result["held"]) == 1

    def test_released_binding_drives_to_default(self, lab):
        # Released binding behaves the same as no binding (drive default).
        _make_binding(lab)  # default desired_state = released

        runtime.apply_safe_drive_on_startup(lab.manager)
        # default_state is open → False
        assert lab.driver.write_log[-1] == (1, False, WriteOutcome.OK)


class TestCheckNoChannelBusyForSbc:
    def test_no_bindings_is_a_no_op(self, lab):
        runtime.check_no_channel_busy_for_sbc(lab.manager, lab.sbc.id)

    def test_passes_when_channel_free(self, lab):
        _make_binding(lab)
        # No verb is in flight, so the check must pass cleanly.
        runtime.check_no_channel_busy_for_sbc(lab.manager, lab.sbc.id)

    def test_raises_when_channel_held(self, lab):
        binding = _make_binding(lab)

        # Hold the per-channel lock manually to simulate a verb in flight.
        lock = runtime._get_channel_lock(binding.actuator_channel_id)
        assert lock.acquire(blocking=False)
        try:
            with pytest.raises(runtime.ChannelBusyError, match="busy"):
                runtime.check_no_channel_busy_for_sbc(lab.manager, lab.sbc.id)
        finally:
            lock.release()

    def test_releases_acquired_locks_on_failure(self, lab):
        """If channel 2 is busy, channel 1's lock must be released after the check."""
        # Two bindings on two channels.
        ch2 = lab.manager.add_actuator_channel(
            lab.actuator.id, 2, default_state=ChannelState.OPEN
        )
        # Bind ch1 to recovery_mode; ch2 to power_button (different SBC purpose).
        b1 = _make_binding(lab, purpose="recovery_mode")
        lab.manager.create_sbc(name="other-sbc")
        # We need both bindings on the SAME sbc to test the mid-loop failure;
        # use a second purpose on lab.sbc.
        b2 = lab.manager.create_binding(
            lab.sbc.id,
            "power_button",
            ch2.id,
            shape_mode=ShapeMode.MOMENTARY,
            shape_active=ChannelState.CLOSED,
            momentary_pulse_ms=200,
        )

        # Hold ch2's lock; ch1's lock must be released after the check.
        ch2_lock = runtime._get_channel_lock(b2.actuator_channel_id)
        assert ch2_lock.acquire(blocking=False)
        try:
            with pytest.raises(runtime.ChannelBusyError):
                runtime.check_no_channel_busy_for_sbc(lab.manager, lab.sbc.id)
        finally:
            ch2_lock.release()

        # Now ch1's lock should be acquirable — proving it was released.
        ch1_lock = runtime._get_channel_lock(b1.actuator_channel_id)
        assert ch1_lock.acquire(blocking=False)
        ch1_lock.release()


class TestAuditLogging:
    """Hardware writes leave a footprint in the activity stream."""

    def _audit_rows(self, manager, action=None, entity_type=None):
        sql = "SELECT action, entity_type, entity_name, result, details FROM audit_log"
        clauses = []
        params: list = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return manager.db.execute(sql, tuple(params))

    def test_actuate_emits_audit_event(self, lab):
        binding = _make_binding(lab)
        runtime.actuate_binding(lab.manager, binding)

        rows = self._audit_rows(lab.manager, action="actuate")
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "binding"
        assert row["entity_name"] == "jetson-nano-2:recovery_mode"
        assert row["result"] == "ok"
        # Details JSON includes the actuator + target state.
        assert "relay-1" in row["details"]
        assert "closed" in row["details"]

    def test_release_emits_audit_event(self, lab):
        binding = _make_binding(lab)
        runtime.actuate_binding(lab.manager, binding)
        runtime.release_binding(lab.manager, binding)

        rows = self._audit_rows(lab.manager, action="release")
        assert len(rows) == 1
        assert rows[0]["entity_name"] == "jetson-nano-2:recovery_mode"

    def test_press_emits_audit_event_with_pulse_ms(self, lab):
        binding = _make_binding(
            lab,
            purpose="power_button",
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=200,
            sample_phase=SamplePhase.NONE,
        )
        runtime.press_binding(lab.manager, binding, sleep_fn=lambda s: None)

        rows = self._audit_rows(lab.manager, action="press")
        assert len(rows) == 1
        assert "200" in rows[0]["details"]  # pulse_ms recorded

    def test_actuate_failure_emits_error_audit(self, lab):
        binding = _make_binding(lab)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE

        with pytest.raises(runtime.ActuationError):
            runtime.actuate_binding(lab.manager, binding)

        rows = self._audit_rows(lab.manager, action="actuate")
        assert len(rows) == 1
        assert rows[0]["result"] == "error"
        assert "device_gone" in rows[0]["details"]

    def test_apply_pre_power_bindings_emits_audit(self, lab):
        """Strap application before a power transition lands in the
        activity stream as 'pre_power_apply' (distinct from 'actuate')."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)

        rows = self._audit_rows(lab.manager, action="pre_power_apply")
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "binding"
        assert row["entity_name"] == "jetson-nano-2:recovery_mode"
        assert row["result"] == "ok"
        assert "asserted" in row["details"]  # desired_state in extras
        assert "closed" in row["details"]  # target state

    def test_apply_pre_power_bindings_records_cycle_changed(self, lab):
        """First pre_power_apply is a real transition; the immediately
        following one (e.g. trailing apply in enter_recovery) records
        cycle_changed=false so the audit log explains the redundant row."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)
        runtime.apply_pre_power_bindings(lab.manager, lab.sbc)

        rows = self._audit_rows(lab.manager, action="pre_power_apply")
        assert len(rows) == 2
        # First is a real transition (last_state was None → CLOSED).
        assert '"cycle_changed":true' in rows[0]["details"]
        # Second re-asserts an already-asserted strap.
        assert '"cycle_changed":false' in rows[1]["details"]

    def test_apply_pre_power_bindings_failure_emits_error_audit(self, lab):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE
        with pytest.raises(runtime.ActuationError):
            runtime.apply_pre_power_bindings(lab.manager, lab.sbc)

        rows = self._audit_rows(lab.manager, action="pre_power_apply")
        assert len(rows) == 1
        assert rows[0]["result"] == "error"
        assert "device_gone" in rows[0]["details"]

    def test_apply_safe_drive_emits_synthetic_audit(self, lab):
        """Daemon-start safe-drive emits one summary 'safe_drive' event
        rather than per-channel events (operators want a single line)."""
        # Add a held binding and one unbound channel for variety.
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        lab.manager.add_actuator_channel(
            lab.actuator.id, 2, default_state=ChannelState.OPEN
        )
        lab.driver._configured_count = 2

        runtime.apply_safe_drive_on_startup(lab.manager)

        rows = self._audit_rows(lab.manager, action="safe_drive")
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "daemon"
        assert row["entity_name"] == "actuators"
        assert row["result"] == "ok"
        # Summary captures both buckets.
        assert "held_count" in row["details"]
        assert "drove_count" in row["details"]
        assert "recovery_mode" in row["details"]  # held purpose preserved

    def test_press_partial_failure_emits_error_audit(self, lab):
        """Press where the second (release) write fails: audit
        captures the failure with the channel-left-in-state hint."""
        binding = _make_binding(
            lab,
            shape_mode=ShapeMode.MOMENTARY,
            momentary_pulse_ms=50,
        )

        original = lab.driver.set_channel
        call_count = [0]

        def flaky(index, *, closed):
            call_count[0] += 1
            if call_count[0] == 2:
                lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE
            return original(index, closed=closed)

        lab.driver.set_channel = flaky

        with pytest.raises(runtime.ActuationError):
            runtime.press_binding(lab.manager, binding, sleep_fn=lambda s: None)

        rows = self._audit_rows(lab.manager, action="press")
        assert len(rows) == 1
        assert rows[0]["result"] == "error"
        # The "left in" diagnostic survives to the audit log.
        assert "left_in_state" in rows[0]["details"]


class TestProbeActuator:
    def test_probe_returns_outcome(self, lab):
        outcome = runtime.probe_actuator(lab.actuator)
        assert outcome.result is ProbeResult.OK

    def test_probe_handles_driver_failure(self, lab, monkeypatch):
        @contextmanager
        def boom(_actuator):
            raise RuntimeError("usb hub yanked")
            yield  # noqa: never reached

        monkeypatch.setattr("labctl.actuators.runtime.open_driver_for", boom)
        outcome = runtime.probe_actuator(lab.actuator)
        assert outcome.result is ProbeResult.UNREACHABLE
        assert outcome.detail and "usb hub" in outcome.detail


class TestPowerHelpers:
    """The shared power-on / power-cycle sequences every path uses (#36)."""

    @pytest.fixture
    def controller(self, lab):
        events = []
        ctrl = MagicMock()
        ctrl.power_off.side_effect = lambda: events.append("off") or True
        ctrl.power_on.side_effect = (
            lambda: events.append(("on", list(lab.driver.write_log))) or True
        )
        ctrl.events = events
        return ctrl

    def test_power_on_drives_strap_first(self, lab, controller):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        assert runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
        assert controller.events == [("on", [(1, True, WriteOutcome.OK)])]

    def test_power_on_drives_released_strap_open(self, lab, controller):
        _make_binding(lab)  # desired_state defaults to RELEASED
        assert runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
        assert controller.events == [("on", [(1, False, WriteOutcome.OK)])]

    def test_power_on_returns_controller_result(self, lab, controller):
        controller.power_on.side_effect = None
        controller.power_on.return_value = False
        assert not runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)

    def test_power_on_strap_failure_leaves_power_alone(self, lab, controller):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        lab.driver.next_write_outcome = WriteOutcome.DEVICE_GONE
        with pytest.raises(runtime.ActuationError, match="device_gone"):
            runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
        controller.power_on.assert_not_called()

    def test_cycle_sequence(self, lab, controller):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        slept = []
        assert runtime.power_cycle_with_bindings(
            lab.manager,
            lab.sbc,
            controller,
            2.5,
            sleep_fn=lambda s: (slept.append(s), controller.events.append("sleep")),
        )
        assert slept == [2.5]
        assert controller.events == [
            "off",
            "sleep",
            ("on", [(1, True, WriteOutcome.OK)]),
        ]

    def test_cycle_refuses_before_power_off(self, lab, controller, monkeypatch):
        from labctl.actuators.base import DriverError

        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        def refuse(transport):
            raise DriverError("permission denied opening /dev/x: hint")

        monkeypatch.setattr(lab.driver, "open", refuse)
        with pytest.raises(runtime.PrePowerCheckError, match="permission denied"):
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
            )
        controller.power_off.assert_not_called()

    def test_cycle_power_off_failure_stops(self, lab, controller):
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        controller.power_off.side_effect = None
        controller.power_off.return_value = False
        with pytest.raises(RuntimeError, match="power_off failed on 'jetson-nano-2'"):
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
            )
        assert lab.driver.write_log == []
        controller.power_on.assert_not_called()

    def test_cycle_strap_write_failure_leaves_board_off(self, lab, controller):
        """The relay opened fine at the check but the write fails: the board
        stays off rather than booting without its strap."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        lab.driver.next_write_outcome = WriteOutcome.WRITE_FAILED
        with pytest.raises(runtime.ActuationError) as exc:
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
            )
        assert not isinstance(exc.value, runtime.PrePowerCheckError)
        assert str(exc.value).endswith("'jetson-nano-2' was left powered off")
        controller.power_off.assert_called_once()
        controller.power_on.assert_not_called()

    def test_no_bindings_is_plain_cycle(self, lab, controller):
        assert runtime.power_cycle_with_bindings(
            lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
        )
        assert controller.events == ["off", ("on", [])]

    @pytest.mark.parametrize(
        "delay",
        [-1, "3", None, True, float("nan"), float("inf"), 1e20, 10**400, 3601],
    )
    def test_cycle_bad_delay_refused_before_anything(self, lab, controller, delay):
        with pytest.raises(
            ValueError, match="delay must be a number of seconds from 0 to 3600"
        ):
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, delay, sleep_fn=lambda s: None
            )
        controller.power_off.assert_not_called()

    def test_busy_strap_channel_is_an_actuation_error(self, lab, controller):
        """Another verb holding the channel in this process (e.g. an MCP
        pulse) is a strap failure like any other, not an unhandled error."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)
        with runtime._channel_lock(lab.channel.id):
            with pytest.raises(
                runtime.PrePowerCheckError,
                match="actuator 'relay-1' channel 1 for 'recovery_mode' is busy",
            ):
                runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
            with pytest.raises(runtime.PrePowerCheckError, match="busy"):
                runtime.power_cycle_with_bindings(
                    lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
                )
        controller.power_off.assert_not_called()
        controller.power_on.assert_not_called()
        assert lab.driver.write_log == []

    def test_channel_taken_after_the_check_leaves_board_off(
        self, lab, controller, monkeypatch
    ):
        """A verb that grabs the channel between the check and the write is
        an ActuationError saying the board is off, not an unhandled error."""
        binding = _make_binding(lab)
        lab.manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)

        def busy(manager, sbc):
            raise runtime.ChannelBusyError("channel 1 is busy")

        monkeypatch.setattr(runtime, "apply_pre_power_bindings", busy)
        with pytest.raises(runtime.ActuationError, match="left powered off"):
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
            )
        controller.power_on.assert_not_called()

    def test_power_on_checks_every_relay_before_driving_any(
        self, lab, controller, monkeypatch
    ):
        """A second strap's relay that can't be used stops power-on before
        the first strap is driven."""
        _make_binding(lab)
        monkeypatch.setattr(
            runtime,
            "probe_actuator",
            lambda a: runtime.ProbeOutcome(
                result=ProbeResult.UNREACHABLE, detail="no such device"
            ),
        )
        with pytest.raises(runtime.PrePowerCheckError, match="no such device"):
            runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
        assert lab.driver.write_log == []
        controller.power_on.assert_not_called()

    def _sharer(self, lab, *, desired=DesiredState.ASSERTED):
        """A second board on the same plug outlet, strapped on channel 2."""
        from labctl.core.models import PlugType

        lab.manager.assign_power_plug(lab.sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
        other = lab.manager.create_sbc(name="board-b")
        lab.manager.assign_power_plug(other.id, PlugType.TASMOTA, "10.0.0.5", 1)
        ch2 = lab.manager.add_actuator_channel(
            lab.actuator.id, 2, default_state=ChannelState.OPEN
        )
        b2 = lab.manager.create_binding(
            other.id,
            "recovery_mode",
            ch2.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
            sample_phase=SamplePhase.PRE_POWER,
        )
        lab.manager.update_binding_desired_state(b2.id, desired)
        lab.driver._configured_count = 2
        lab.sbc = lab.manager.get_sbc_by_name(lab.sbc.name)
        return other

    def test_sharer_refusal_names_the_sharer(self, lab, controller, monkeypatch):
        self._sharer(lab)  # only the sharer has a strap
        monkeypatch.setattr(
            runtime,
            "probe_actuator",
            lambda a: runtime.ProbeOutcome(
                result=ProbeResult.UNREACHABLE, detail="gone"
            ),
        )
        with pytest.raises(runtime.PrePowerCheckError) as exc:
            runtime.power_on_with_bindings(lab.manager, lab.sbc, controller)
        assert str(exc.value).endswith(
            "('board-b' shares 'jetson-nano-2''s power outlet)"
        )

    def test_left_off_message_names_sharers(self, lab, controller):
        _make_binding(lab)
        self._sharer(lab)
        lab.driver.next_write_outcome = WriteOutcome.WRITE_FAILED
        with pytest.raises(runtime.ActuationError) as exc:
            runtime.power_cycle_with_bindings(
                lab.manager, lab.sbc, controller, 0, sleep_fn=lambda s: None
            )
        assert str(exc.value).endswith(
            "'jetson-nano-2' and the boards sharing its outlet ('board-b') "
            "were left powered off"
        )

    def test_recovery_sets_sharers_straps(self, lab, controller):
        _make_binding(lab)
        self._sharer(lab)
        runtime.enter_recovery(
            lab.manager, lab.sbc, controller, delay_s=0, sleep_fn=lambda s: None
        )
        ((_, log),) = [e for e in controller.events if isinstance(e, tuple)]
        assert (1, True, WriteOutcome.OK) in log
        assert (2, True, WriteOutcome.OK) in log

    def test_recovery_sharer_strap_failure_says_boards_left_off(
        self, lab, controller, monkeypatch
    ):
        _make_binding(lab)
        other = self._sharer(lab)
        real_apply = runtime.apply_pre_power_bindings

        def fail_for_sharer(manager, board):
            if board.name == other.name:
                raise runtime.ActuationError("pre_power binding: device_gone")
            real_apply(manager, board)

        monkeypatch.setattr(runtime, "apply_pre_power_bindings", fail_for_sharer)
        with pytest.raises(runtime.ActuationError) as exc:
            runtime.enter_recovery(
                lab.manager, lab.sbc, controller, delay_s=0, sleep_fn=lambda s: None
            )
        assert str(exc.value).endswith(
            "'jetson-nano-2' and the boards sharing its outlet ('board-b') "
            "were left powered off"
        )
        controller.power_on.assert_not_called()
