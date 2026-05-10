"""Runtime layer between the manager's persistent state and drivers.

Implements the user-facing verbs (`actuate`, `release`, `press`, etc.)
in terms of :class:`labctl.actuators.base.RelayDriver` calls, with:

  * Per-channel non-blocking locks so concurrent verbs on the same
    channel surface a clean "channel busy" error rather than racing.
  * Verb/shape congruence enforcement (`actuate`/`release` need a
    latch binding; `press` needs a momentary binding) — see
    docs/SPEC_actuators.md §"Verb / shape congruence".
  * Atomic state updates: a successful write stamps `last_state`,
    bumps `cycle_count`, and updates `desired_state` in that order.
    Failed writes leave persistent state untouched, so a daemon
    restart's safe-drive logic (Phase 5) sees the truth.

Used by the CLI (`labctl actuate / release / press / bindings status`)
and, in Phase 7, the MCP server.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Optional

from labctl.actuators import get_driver
from labctl.actuators.base import (
    ProbeOutcome,
    ProbeResult,
    RelayDriver,
    Transport,
    WriteOutcome,
)
from labctl.core.models import (
    Actuator,
    ActuatorChannel,
    Binding,
    ChannelState,
    DesiredState,
    ShapeMode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ActuationError(Exception):
    """Driver-level failure during a verb (e.g. WRITE_FAILED, DEVICE_GONE)."""


class BindingShapeError(Exception):
    """Verb does not match the binding's declared shape."""


class ChannelBusyError(Exception):
    """Per-channel lock could not be acquired (another verb is in flight)."""


# ---------------------------------------------------------------------------
# Per-channel locks (process-local)
# ---------------------------------------------------------------------------


_channel_locks: dict[int, threading.Lock] = {}
_channel_locks_meta = threading.Lock()


def _get_channel_lock(channel_id: int) -> threading.Lock:
    with _channel_locks_meta:
        lock = _channel_locks.get(channel_id)
        if lock is None:
            lock = threading.Lock()
            _channel_locks[channel_id] = lock
        return lock


def _clear_runtime_state() -> None:
    """Test-only: drop all per-channel locks."""
    with _channel_locks_meta:
        _channel_locks.clear()


@contextmanager
def _channel_lock(channel_id: int):
    """Non-blocking acquire; raise :class:`ChannelBusyError` if held."""
    lock = _get_channel_lock(channel_id)
    if not lock.acquire(blocking=False):
        raise ChannelBusyError(
            f"channel {channel_id} is busy (another operation in flight)"
        )
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Driver helpers
# ---------------------------------------------------------------------------


def _build_transport(actuator: Actuator) -> Transport:
    return Transport(
        device_path=actuator.device_path,
        vid=actuator.vid,
        pid=actuator.pid,
        serial_no=actuator.serial_no,
    )


@contextmanager
def open_driver_for(actuator: Actuator) -> RelayDriver:
    """Open a driver for ``actuator`` and close it on exit."""
    driver = get_driver(
        actuator.driver, expected_channel_count=len(actuator.channels) or None
    )
    transport = _build_transport(actuator)
    driver.open(transport)
    try:
        yield driver
    finally:
        driver.close()


def _opposite(state: ChannelState) -> ChannelState:
    return (
        ChannelState.OPEN if state is ChannelState.CLOSED else ChannelState.CLOSED
    )


def _drive(
    driver: RelayDriver, channel_index: int, target: ChannelState
) -> WriteOutcome:
    return driver.set_channel(channel_index, closed=target is ChannelState.CLOSED)


# ---------------------------------------------------------------------------
# Verb implementations
# ---------------------------------------------------------------------------


def _resolve(manager, binding: Binding) -> tuple[Actuator, ActuatorChannel]:
    """Load the actuator + channel referenced by a binding.

    Returns the channel record (so the caller can stamp last_state).
    """
    channel_row = manager.db.execute_one(
        "SELECT * FROM actuator_channels WHERE id = ?",
        (binding.actuator_channel_id,),
    )
    if not channel_row:
        raise ActuationError(
            f"binding {binding.id} references missing channel "
            f"{binding.actuator_channel_id}"
        )
    channel = ActuatorChannel.from_row(channel_row)
    actuator = manager.get_actuator(channel.actuator_id)
    if actuator is None:
        raise ActuationError(
            f"binding {binding.id} references missing actuator "
            f"{channel.actuator_id}"
        )
    return actuator, channel


def _require_shape(binding: Binding, expected: ShapeMode, verb: str) -> None:
    if binding.shape_mode is not expected:
        if expected is ShapeMode.LATCH:
            hint = (
                f"Use 'press' for momentary bindings, or rebind with "
                f"--mode latch for sustained state."
            )
        else:
            hint = (
                f"Use 'actuate'/'release' for latch bindings, or rebind "
                f"with --mode momentary if you want press semantics."
            )
        raise BindingShapeError(
            f"{verb!r} requires a {expected.value} binding; "
            f"{binding.purpose!r} is bound as {binding.shape_mode.value}. {hint}"
        )


def actuate_binding(manager, binding: Binding) -> None:
    """Drive a latch binding to its active state and persist intent."""
    _require_shape(binding, ShapeMode.LATCH, "actuate")
    actuator, channel = _resolve(manager, binding)
    target = binding.shape_active

    with _channel_lock(channel.id):
        with open_driver_for(actuator) as driver:
            outcome = _drive(driver, channel.channel_index, target)
        if outcome is not WriteOutcome.OK:
            raise ActuationError(
                f"actuate failed: driver returned {outcome.value}"
            )
        manager.update_channel_state(channel.id, target)
        manager.update_binding_desired_state(binding.id, DesiredState.ASSERTED)


def release_binding(manager, binding: Binding) -> None:
    """Drive a latch binding away from its active state."""
    _require_shape(binding, ShapeMode.LATCH, "release")
    actuator, channel = _resolve(manager, binding)
    target = _opposite(binding.shape_active)

    with _channel_lock(channel.id):
        with open_driver_for(actuator) as driver:
            outcome = _drive(driver, channel.channel_index, target)
        if outcome is not WriteOutcome.OK:
            raise ActuationError(
                f"release failed: driver returned {outcome.value}"
            )
        manager.update_channel_state(channel.id, target)
        manager.update_binding_desired_state(binding.id, DesiredState.RELEASED)


def press_binding(
    manager, binding: Binding, *, sleep_fn=time.sleep
) -> None:
    """Pulse a momentary binding to active for momentary_pulse_ms then back.

    Both writes must succeed. If the second write fails the channel may
    be left asserted — we surface that as :class:`ActuationError` so
    the operator can intervene; ``last_state`` reflects the last
    successful drive.
    """
    _require_shape(binding, ShapeMode.MOMENTARY, "press")
    if not binding.momentary_pulse_ms or binding.momentary_pulse_ms <= 0:
        raise ActuationError(
            f"binding {binding.purpose!r} has no momentary_pulse_ms"
        )

    actuator, channel = _resolve(manager, binding)
    active = binding.shape_active
    inactive = _opposite(active)

    with _channel_lock(channel.id):
        with open_driver_for(actuator) as driver:
            out1 = _drive(driver, channel.channel_index, active)
            if out1 is not WriteOutcome.OK:
                raise ActuationError(
                    f"press: failed to assert: {out1.value}"
                )
            manager.update_channel_state(channel.id, active)

            sleep_fn(binding.momentary_pulse_ms / 1000.0)

            out2 = _drive(driver, channel.channel_index, inactive)
        if out2 is not WriteOutcome.OK:
            raise ActuationError(
                f"press: assert ok, but release failed ({out2.value}). "
                f"Channel may be left in {active.value} state."
            )
        manager.update_channel_state(channel.id, inactive)


# ---------------------------------------------------------------------------
# Status / inspection
# ---------------------------------------------------------------------------


def binding_status(manager, binding: Binding) -> dict:
    """Return a dict describing the current binding state for display.

    Does not touch the hardware — pure DB read. Combine with
    :func:`probe_actuator` if you also want a fresh reachability check.
    """
    actuator, channel = _resolve(manager, binding)
    return {
        "binding": {
            "sbc": binding.sbc_name,
            "purpose": binding.purpose,
            "shape_mode": binding.shape_mode.value,
            "shape_active": binding.shape_active.value,
            "sample_phase": binding.sample_phase.value,
            "momentary_pulse_ms": binding.momentary_pulse_ms,
            "desired_state": binding.desired_state.value,
        },
        "actuator": {
            "name": actuator.name,
            "driver": actuator.driver.value,
            "device_path": actuator.device_path,
        },
        "channel": {
            "index": channel.channel_index,
            "default_state": channel.default_state.value,
            "last_state": (
                channel.last_state.value if channel.last_state else None
            ),
            "last_changed_at": (
                channel.last_changed_at.isoformat()
                if channel.last_changed_at
                else None
            ),
            "cycle_count": channel.cycle_count,
        },
    }


def probe_actuator(actuator: Actuator) -> ProbeOutcome:
    """Probe an actuator and return the outcome (read-only)."""
    try:
        with open_driver_for(actuator) as driver:
            return driver.probe()
    except Exception as e:  # noqa: BLE001
        return ProbeOutcome(
            result=ProbeResult.UNREACHABLE, detail=str(e)
        )
