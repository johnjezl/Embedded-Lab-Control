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


def drop_channel_lock(channel_id: int) -> None:
    """Drop the per-channel lock entry, if any.

    Called when a channel (or its parent actuator) is deleted so the
    process-local lock dict doesn't accumulate stale entries across
    reprovisioning. Safe to call for IDs that never had a lock.
    """
    with _channel_locks_meta:
        _channel_locks.pop(channel_id, None)


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


def check_no_channel_busy_for_sbc(manager, sbc_id: int) -> None:
    """Snapshot check: raise :class:`ChannelBusyError` if any binding's
    channel is currently held by an in-flight verb.

    Called from ``claim_sbc`` so a claim can't sneak in while another
    process is mid-actuate. Locks are released immediately after the
    check — claim ownership across processes is enforced by the DB-
    backed claim, not by these in-memory locks.
    """
    bindings = manager.list_bindings(sbc_id=sbc_id)
    acquired: list[threading.Lock] = []
    busy: Optional[Binding] = None
    try:
        for b in bindings:
            lock = _get_channel_lock(b.actuator_channel_id)
            if lock.acquire(blocking=False):
                acquired.append(lock)
            else:
                busy = b
                break
    finally:
        for lock in acquired:
            lock.release()
    if busy is not None:
        label = (
            f"{busy.actuator_name}[{busy.channel_index}]"
            if busy.actuator_name
            else f"channel id {busy.actuator_channel_id}"
        )
        raise ChannelBusyError(
            f"actuator channel {label} is busy "
            f"(binding {busy.purpose!r} in flight)"
        )


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


def _audit_actuator_event(
    manager,
    action: str,
    binding: Binding,
    actuator: Actuator,
    channel: ActuatorChannel,
    target: ChannelState,
    *,
    ok: bool,
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Emit an audit_log row for a binding-mediated hardware write.

    Hardware writes are operationally significant and the activity
    stream is the project's "who did what to which device" log. Every
    successful or failed actuate/release/press lands here.
    """
    from labctl.core import audit

    details: dict = {
        "actuator": actuator.name,
        "channel": channel.channel_index,
        "target_state": target.value,
    }
    if extra:
        details.update(extra)
    if error is not None:
        details["error"] = error
    audit.emit(
        manager.db,
        action=action,
        entity_type="binding",
        entity_id=binding.id,
        entity_name=(
            f"{binding.sbc_name}:{binding.purpose}"
            if binding.sbc_name
            else binding.purpose
        ),
        result="ok" if ok else "error",
        details=details,
    )


def _audit_raw_actuator_set(
    manager,
    actuator: Actuator,
    channel: ActuatorChannel,
    target: ChannelState,
    *,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    """Audit-log a raw `actuator set` that bypasses any binding."""
    from labctl.core import audit

    details: dict = {
        "channel": channel.channel_index,
        "target_state": target.value,
        "raw": True,
    }
    if error is not None:
        details["error"] = error
    audit.emit(
        manager.db,
        action="actuator_set",
        entity_type="actuator_channel",
        entity_id=channel.id,
        entity_name=f"{actuator.name}[{channel.channel_index}]",
        result="ok" if ok else "error",
        details=details,
    )


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
            _audit_actuator_event(
                manager,
                "actuate",
                binding,
                actuator,
                channel,
                target,
                ok=False,
                error=outcome.value,
            )
            raise ActuationError(
                f"actuate failed: driver returned {outcome.value}"
            )
        # Atomic: channel state + binding desired_state in one transaction.
        manager.commit_actuation(
            channel.id, target, binding.id, DesiredState.ASSERTED
        )
        _audit_actuator_event(
            manager,
            "actuate",
            binding,
            actuator,
            channel,
            target,
            ok=True,
        )


def release_binding(manager, binding: Binding) -> None:
    """Drive a latch binding away from its active state."""
    _require_shape(binding, ShapeMode.LATCH, "release")
    actuator, channel = _resolve(manager, binding)
    target = _opposite(binding.shape_active)

    with _channel_lock(channel.id):
        with open_driver_for(actuator) as driver:
            outcome = _drive(driver, channel.channel_index, target)
        if outcome is not WriteOutcome.OK:
            _audit_actuator_event(
                manager,
                "release",
                binding,
                actuator,
                channel,
                target,
                ok=False,
                error=outcome.value,
            )
            raise ActuationError(
                f"release failed: driver returned {outcome.value}"
            )
        manager.commit_actuation(
            channel.id, target, binding.id, DesiredState.RELEASED
        )
        _audit_actuator_event(
            manager,
            "release",
            binding,
            actuator,
            channel,
            target,
            ok=True,
        )


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
                _audit_actuator_event(
                    manager,
                    "press",
                    binding,
                    actuator,
                    channel,
                    active,
                    ok=False,
                    error=f"assert: {out1.value}",
                    extra={"pulse_ms": binding.momentary_pulse_ms},
                )
                raise ActuationError(
                    f"press: failed to assert: {out1.value}"
                )
            manager.update_channel_state(channel.id, active)

            sleep_fn(binding.momentary_pulse_ms / 1000.0)

            out2 = _drive(driver, channel.channel_index, inactive)
        if out2 is not WriteOutcome.OK:
            _audit_actuator_event(
                manager,
                "press",
                binding,
                actuator,
                channel,
                inactive,
                ok=False,
                error=f"release: {out2.value}",
                extra={
                    "pulse_ms": binding.momentary_pulse_ms,
                    "left_in_state": active.value,
                },
            )
            raise ActuationError(
                f"press: assert ok, but release failed ({out2.value}). "
                f"Channel may be left in {active.value} state."
            )
        manager.update_channel_state(channel.id, inactive)
        _audit_actuator_event(
            manager,
            "press",
            binding,
            actuator,
            channel,
            inactive,
            ok=True,
            extra={"pulse_ms": binding.momentary_pulse_ms},
        )


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


# ---------------------------------------------------------------------------
# Power-aware integration (Phase 5)
# ---------------------------------------------------------------------------


def _bindings_with_sample_phase(manager, sbc_id: int, phase) -> list[Binding]:
    """Return bindings on ``sbc_id`` whose sample_phase matches ``phase``."""
    return [
        b
        for b in manager.list_bindings(sbc_id=sbc_id)
        if b.sample_phase == phase
    ]


def apply_pre_power_bindings(manager, sbc) -> None:
    """Drive every pre_power binding on ``sbc`` to match its desired_state.

    Called BEFORE ``power_on`` / ``power_cycle`` so a held recovery_mode
    strap is in place before the device boots. No-op for SBCs with no
    pre_power bindings.

    Raises :class:`ActuationError` on any driver failure — power must
    NOT proceed without the strap in place.
    """
    from labctl.core.models import SamplePhase

    pre_bindings = _bindings_with_sample_phase(
        manager, sbc.id, SamplePhase.PRE_POWER
    )
    for binding in pre_bindings:
        actuator, channel = _resolve(manager, binding)
        if binding.desired_state == DesiredState.ASSERTED:
            target = binding.shape_active
        elif binding.desired_state == DesiredState.RELEASED:
            target = _opposite(binding.shape_active)
        else:
            # FOLLOWING_POWER — driven by the binding's own logic post-on.
            continue

        with _channel_lock(channel.id):
            with open_driver_for(actuator) as driver:
                outcome = _drive(driver, channel.channel_index, target)
            if outcome is not WriteOutcome.OK:
                raise ActuationError(
                    f"pre_power binding {binding.purpose!r} on "
                    f"{binding.sbc_name!r}: {outcome.value}"
                )
            manager.update_channel_state(channel.id, target)


def enter_recovery(
    manager,
    sbc,
    controller,
    *,
    delay_s: float,
    sleep_fn=time.sleep,
) -> None:
    """Power-aware "enter USB Force Recovery (or equivalent)" composite.

    Sequence (see SPEC_actuators.md §"Composite, power-aware operations"):

      0. Pre-flight probe every actuator we'll touch — abort BEFORE
         power_off if any is unreachable.
      1. power_off
      2. sleep(delay_s)
      3. actuate recovery_mode  → desired=asserted, strap engaged
      4. power_on (consults pre_power bindings; idempotent for the
         strap we just engaged)
    """
    binding = manager.get_binding_by_target(sbc.id, "recovery_mode")
    if binding is None:
        raise ActuationError(
            f"no 'recovery_mode' binding on {sbc.name!r}"
        )
    actuator, _channel = _resolve(manager, binding)

    # 0. Pre-flight probe. Abort before any power transition.
    outcome = probe_actuator(actuator)
    if outcome.result is not ProbeResult.OK:
        raise ActuationError(
            f"pre-flight probe failed on {actuator.name!r}: "
            f"{outcome.result.value} ({outcome.detail or 'no detail'})"
        )

    # 1. Power off.
    if not controller.power_off():
        raise ActuationError(f"power_off failed on {sbc.name!r}")
    # 2. Settling delay.
    sleep_fn(delay_s)
    # 3. Engage recovery strap.
    actuate_binding(manager, binding)
    # 4. Power on (re-applies the strap via apply_pre_power_bindings).
    apply_pre_power_bindings(manager, sbc)
    if not controller.power_on():
        raise ActuationError(f"power_on failed on {sbc.name!r}")


def exit_recovery(
    manager,
    sbc,
    controller,
    *,
    delay_s: float,
    sleep_fn=time.sleep,
) -> None:
    """Power-aware "leave recovery" composite.

    Same shape as :func:`enter_recovery` but releases the strap.
    """
    binding = manager.get_binding_by_target(sbc.id, "recovery_mode")
    if binding is None:
        raise ActuationError(
            f"no 'recovery_mode' binding on {sbc.name!r}"
        )
    actuator, _channel = _resolve(manager, binding)

    outcome = probe_actuator(actuator)
    if outcome.result is not ProbeResult.OK:
        raise ActuationError(
            f"pre-flight probe failed on {actuator.name!r}: "
            f"{outcome.result.value} ({outcome.detail or 'no detail'})"
        )

    if not controller.power_off():
        raise ActuationError(f"power_off failed on {sbc.name!r}")
    sleep_fn(delay_s)
    release_binding(manager, binding)
    apply_pre_power_bindings(manager, sbc)
    if not controller.power_on():
        raise ActuationError(f"power_on failed on {sbc.name!r}")


# ---------------------------------------------------------------------------
# Daemon-start safe drive
# ---------------------------------------------------------------------------


def apply_safe_drive_on_startup(manager) -> dict:
    """Reconcile every actuator channel against its binding's desired_state.

    On daemon start every channel is evaluated:

      * No binding for channel              → drive to default_state.
      * Binding desired_state=released       → drive to default_state.
      * Binding desired_state=asserted       → LEAVE ALONE, warn.
      * Binding desired_state=following_power → leave alone (driven later
        by power_on consulting pre_power bindings).

    The "leave alone" path is the keystone: a recovery_mode strap held
    mid-flash MUST survive daemon restarts. Snapping it to default_state
    would drop the flash. The warning is the operator's signal that
    something was mid-operation when the daemon went down.

    Returns a dict summarising what happened (test/observability hook).
    """
    bindings_by_channel: dict[int, Binding] = {
        b.actuator_channel_id: b for b in manager.list_bindings()
    }

    held: list[dict] = []
    drove: list[dict] = []
    failed: list[dict] = []

    for actuator in manager.list_actuators():
        _safe_drive_one_actuator(
            manager, actuator, bindings_by_channel,
            held=held, drove=drove, failed=failed,
        )

    return {"held": held, "drove": drove, "failed": failed}


def _safe_drive_one_actuator(
    manager,
    actuator: Actuator,
    bindings_by_channel: dict[int, Binding],
    *,
    held: list[dict],
    drove: list[dict],
    failed: list[dict],
) -> None:
    """Reconcile one actuator's channels; appends to held/drove/failed.

    Skips entirely if the actuator can't be opened (logs a warning). For
    each channel: leave alone if its binding is held, otherwise drive
    to the channel's default_state.
    """
    try:
        cm = open_driver_for(actuator)
        driver = cm.__enter__()
    except Exception as e:  # noqa: BLE001
        logger.warning("safe-drive: cannot open %s: %s", actuator.name, e)
        return
    try:
        for ch in actuator.channels:
            binding = bindings_by_channel.get(ch.id)
            if binding is not None and binding.desired_state in (
                DesiredState.ASSERTED,
                DesiredState.FOLLOWING_POWER,
            ):
                held.append(
                    {
                        "actuator": actuator.name,
                        "channel": ch.channel_index,
                        "purpose": binding.purpose,
                        "desired_state": binding.desired_state.value,
                    }
                )
                logger.warning(
                    "safe-drive: leaving %s[%d] alone "
                    "(binding %s desired=%s)",
                    actuator.name,
                    ch.channel_index,
                    binding.purpose,
                    binding.desired_state.value,
                )
                continue

            target = ch.default_state
            outcome = _drive(driver, ch.channel_index, target)
            if outcome is WriteOutcome.OK:
                manager.update_channel_state(ch.id, target)
                drove.append(
                    {
                        "actuator": actuator.name,
                        "channel": ch.channel_index,
                        "target": target.value,
                    }
                )
            else:
                failed.append(
                    {
                        "actuator": actuator.name,
                        "channel": ch.channel_index,
                        "outcome": outcome.value,
                    }
                )
                logger.warning(
                    "safe-drive: write to %s[%d] returned %s",
                    actuator.name,
                    ch.channel_index,
                    outcome.value,
                )
    finally:
        cm.__exit__(None, None, None)
