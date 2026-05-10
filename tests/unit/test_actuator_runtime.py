"""Tests for the actuator runtime layer (actuate/release/press/status)."""

from __future__ import annotations

import threading
from contextlib import contextmanager

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
            runtime.press_binding(
                lab.manager, binding, sleep_fn=lambda s: None
            )

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


class TestProbeActuator:
    def test_probe_returns_outcome(self, lab):
        outcome = runtime.probe_actuator(lab.actuator)
        assert outcome.result is ProbeResult.OK

    def test_probe_handles_driver_failure(self, lab, monkeypatch):
        @contextmanager
        def boom(_actuator):
            raise RuntimeError("usb hub yanked")
            yield  # noqa: never reached

        monkeypatch.setattr(
            "labctl.actuators.runtime.open_driver_for", boom
        )
        outcome = runtime.probe_actuator(lab.actuator)
        assert outcome.result is ProbeResult.UNREACHABLE
        assert outcome.detail and "usb hub" in outcome.detail
