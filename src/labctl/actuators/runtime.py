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
    DriverError,
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


class PrePowerCheckError(ActuationError):
    """A pre_power actuator can't be used; power was not switched."""


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
            f"actuator channel {label} is busy " f"(binding {busy.purpose!r} in flight)"
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
    """Open a driver for ``actuator`` and close it on exit.

    A driver that isn't implemented or can't open its device raises
    :class:`ActuationError`, which the verbs' callers report, rather than
    a bare DriverError or NotImplementedError.
    """
    try:
        driver = get_driver(
            actuator.driver, expected_channel_count=len(actuator.channels) or None
        )
        driver.open(_build_transport(actuator))
    except (DriverError, NotImplementedError) as e:
        raise ActuationError(str(e)) from e
    try:
        yield driver
    finally:
        driver.close()


def _opposite(state: ChannelState) -> ChannelState:
    return ChannelState.OPEN if state is ChannelState.CLOSED else ChannelState.CLOSED


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

    Defensive: any failure here is logged but never raised — a verb
    that physically succeeded must not be reported as failed because
    audit bookkeeping went sideways.
    """
    try:
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
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "audit emit failed for action=%s binding=%s: %s",
            action,
            binding.purpose,
            e,
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
    try:
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
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "audit emit failed for actuator_set %s[%d]: %s",
            actuator.name,
            channel.channel_index,
            e,
        )


def _audit_safe_drive(
    manager, summary: dict, *, ok: bool, error: Optional[str] = None
) -> None:
    """Emit a synthetic 'safe_drive' audit event summarizing daemon-start
    reconciliation: which channels were left held, which were driven to
    default, and which writes failed. This is the activity-stream
    counterpart to the per-channel logger.warning lines."""
    try:
        from labctl.core import audit

        details: dict = {
            "held_count": len(summary.get("held", [])),
            "drove_count": len(summary.get("drove", [])),
            "failed_count": len(summary.get("failed", [])),
            "held": summary.get("held", []),
            "drove": summary.get("drove", []),
            "failed": summary.get("failed", []),
        }
        if error is not None:
            details["error"] = error
        audit.emit(
            manager.db,
            action="safe_drive",
            entity_type="daemon",
            entity_name="actuators",
            result="ok" if ok else "error",
            details=details,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit emit failed for safe_drive: %s", e)


def _audit_pre_power_apply(
    manager,
    binding: Binding,
    actuator: Actuator,
    channel: ActuatorChannel,
    target: ChannelState,
    *,
    ok: bool,
    error: Optional[str] = None,
    cycle_changed: Optional[bool] = None,
) -> None:
    """Emit a 'pre_power_apply' audit event for a strap driven before a
    power transition. Distinct from 'actuate' so the activity stream
    distinguishes operator-initiated verbs from automatic power-flow
    re-application.

    ``cycle_changed`` indicates whether the channel was already in the
    target state pre-write (False) or required a real transition (True).
    Lets operators read paired actuate→pre_power events without thinking
    the strap was double-toggled.
    """
    extra: dict = {"desired_state": binding.desired_state.value}
    if cycle_changed is not None:
        extra["cycle_changed"] = cycle_changed
    _audit_actuator_event(
        manager,
        "pre_power_apply",
        binding,
        actuator,
        channel,
        target,
        ok=ok,
        error=error,
        extra=extra,
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
                "Use 'press' for momentary bindings, or rebind with "
                "--mode latch for sustained state."
            )
        else:
            hint = (
                "Use 'actuate'/'release' for latch bindings, or rebind "
                "with --mode momentary if you want press semantics."
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
            raise ActuationError(f"actuate failed: driver returned {outcome.value}")
        # Atomic: channel state + binding desired_state in one transaction.
        manager.commit_actuation(channel.id, target, binding.id, DesiredState.ASSERTED)
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
            raise ActuationError(f"release failed: driver returned {outcome.value}")
        manager.commit_actuation(channel.id, target, binding.id, DesiredState.RELEASED)
        _audit_actuator_event(
            manager,
            "release",
            binding,
            actuator,
            channel,
            target,
            ok=True,
        )


def press_binding(manager, binding: Binding, *, sleep_fn=time.sleep) -> None:
    """Pulse a momentary binding to active for momentary_pulse_ms then back.

    Both writes must succeed. If the second write fails the channel may
    be left asserted — we surface that as :class:`ActuationError` so
    the operator can intervene; ``last_state`` reflects the last
    successful drive.
    """
    _require_shape(binding, ShapeMode.MOMENTARY, "press")
    if not binding.momentary_pulse_ms or binding.momentary_pulse_ms <= 0:
        raise ActuationError(f"binding {binding.purpose!r} has no momentary_pulse_ms")

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
                raise ActuationError(f"press: failed to assert: {out1.value}")
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
            "last_state": (channel.last_state.value if channel.last_state else None),
            "last_changed_at": (
                channel.last_changed_at.isoformat() if channel.last_changed_at else None
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
        return ProbeOutcome(result=ProbeResult.UNREACHABLE, detail=str(e))


# ---------------------------------------------------------------------------
# Power-aware integration (Phase 5)
# ---------------------------------------------------------------------------


def _bindings_with_sample_phase(manager, sbc_id: int, phase) -> list[Binding]:
    """Return bindings on ``sbc_id`` whose sample_phase matches ``phase``."""
    return [b for b in manager.list_bindings(sbc_id=sbc_id) if b.sample_phase == phase]


def check_pre_power_actuators(manager, sbc) -> None:
    """Raise :class:`PrePowerCheckError` if an actuator that
    apply_pre_power_bindings will drive for ``sbc`` can't be opened.

    For power cycle, so a relay that would fail after power-off (e.g. no
    permission) is found while the board is still running. Read-only: the
    probe opens and closes the device without touching a channel.
    """
    from labctl.core.models import SamplePhase

    seen = set()
    for binding in _bindings_with_sample_phase(manager, sbc.id, SamplePhase.PRE_POWER):
        # Resolved first, as apply_pre_power_bindings does, so a binding
        # to a missing channel fails here rather than after power-off.
        try:
            actuator, channel = _resolve(manager, binding)
        except ActuationError as e:
            raise PrePowerCheckError(str(e)) from e
        if binding.desired_state not in (
            DesiredState.ASSERTED,
            DesiredState.RELEASED,
        ):
            continue
        if _get_channel_lock(channel.id).locked():
            raise PrePowerCheckError(
                f"actuator {actuator.name!r} channel {channel.channel_index} "
                f"for {binding.purpose!r} is busy (another operation in "
                f"flight)"
            )
        if actuator.id in seen:
            continue
        seen.add(actuator.id)
        outcome = probe_actuator(actuator)
        if outcome.result is not ProbeResult.OK:
            raise PrePowerCheckError(
                f"actuator {actuator.name!r} for {binding.purpose!r} can't "
                f"be used: {outcome.detail or outcome.result.value}"
            )


def apply_pre_power_bindings(manager, sbc) -> None:
    """Drive every pre_power binding on ``sbc`` to match its desired_state.

    Called BEFORE ``power_on`` / ``power_cycle`` so a held recovery_mode
    strap is in place before the device boots. No-op for SBCs with no
    pre_power bindings.

    Raises :class:`ActuationError` on any driver failure — power must
    NOT proceed without the strap in place.
    """
    from labctl.core.models import SamplePhase

    pre_bindings = _bindings_with_sample_phase(manager, sbc.id, SamplePhase.PRE_POWER)
    for binding in pre_bindings:
        actuator, channel = _resolve(manager, binding)
        if binding.desired_state == DesiredState.ASSERTED:
            target = binding.shape_active
        elif binding.desired_state == DesiredState.RELEASED:
            target = _opposite(binding.shape_active)
        else:
            # FOLLOWING_POWER — driven by the binding's own logic post-on.
            continue

        # Capture pre-write state for cycle_changed before the drive +
        # update_channel_state pair invalidates it.
        prior_state = channel.last_state
        with _channel_lock(channel.id):
            with open_driver_for(actuator) as driver:
                outcome = _drive(driver, channel.channel_index, target)
            if outcome is not WriteOutcome.OK:
                _audit_pre_power_apply(
                    manager,
                    binding,
                    actuator,
                    channel,
                    target,
                    ok=False,
                    error=outcome.value,
                )
                raise ActuationError(
                    f"pre_power binding {binding.purpose!r} on "
                    f"{binding.sbc_name!r}: {outcome.value}"
                )
            manager.update_channel_state(channel.id, target)
            _audit_pre_power_apply(
                manager,
                binding,
                actuator,
                channel,
                target,
                ok=True,
                cycle_changed=(prior_state != target),
            )


def boards_on_plug(manager, sbc) -> list:
    """``sbc`` and every other SBC on its plug outlet: switching the plug
    powers them all, so each one's straps must be set first."""
    boards = [sbc]
    plug = sbc.power_plug
    if plug is None:
        return boards
    for name in manager.sbcs_sharing_hardware(
        plug=(plug.plug_type.value, plug.address, plug.plug_index)
    ):
        if name != sbc.name:
            other = manager.get_sbc_by_name(name)
            if other is not None:
                boards.append(other)
    return boards


MAX_CYCLE_DELAY_SECONDS = 86400.0


def validate_cycle_delay(delay_s) -> None:
    """Raise ValueError unless ``delay_s`` is a number of seconds from 0
    to :data:`MAX_CYCLE_DELAY_SECONDS` (time.sleep can't take much more,
    and a cycle validates before it switches anything)."""
    try:
        ok = (
            not isinstance(delay_s, bool)
            and isinstance(delay_s, (int, float))
            and 0 <= float(delay_s) <= MAX_CYCLE_DELAY_SECONDS
        )
    except OverflowError:  # an int too large for a float
        ok = False
    if not ok:
        raise ValueError(
            f"delay must be a number of seconds from 0 to "
            f"{MAX_CYCLE_DELAY_SECONDS:g}, not {delay_s!r}"
        )


DEFAULT_CYCLE_DELAY_SECONDS = 3.0


class StoredCycleDelayError(ValueError):
    """The cycle delay stored for a board is out of range (it was stored
    before ``edit --cycle-delay`` checked the range)."""


def stored_cycle_delay(sbc) -> Optional[float]:
    """The board's stored power-cycle delay, or None when none is set.

    Raises :class:`StoredCycleDelayError` (a ValueError) when the stored
    value is out of range, so no path ever sleeps on it."""
    value = getattr(sbc, "power_cycle_delay_seconds", None)
    if value is None:
        return None
    try:
        validate_cycle_delay(value)
    except ValueError:
        raise StoredCycleDelayError(
            f"the cycle delay stored for {sbc.name!r} ({value!r}) is out of "
            f"range (0 to {MAX_CYCLE_DELAY_SECONDS:g} seconds); clear it with: "
            f"labctl edit {sbc.name} --cycle-delay -1"
        ) from None
    return float(value)


def cycle_delay_floor(manager, sbc) -> tuple[float, str]:
    """The minimum delay of a power cycle of ``sbc`` and where it comes
    from.

    A cycle switches the board's plug outlet, so every board on that
    outlet is cycled: the floor is the largest stored delay among them
    (``"SBC minimum"`` for ``sbc``'s own, or ``"minimum of '<other>'
    (on the same outlet)"``), or :data:`DEFAULT_CYCLE_DELAY_SECONDS` when
    none of them has one (``"default minimum"``). Raises
    :class:`StoredCycleDelayError` when any of their stored delays is out
    of range.
    """
    boards = (
        boards_on_plug(manager, sbc)
        if getattr(sbc, "power_plug", None) is not None
        else [sbc]
    )
    best: tuple[float, str] | None = None
    for board in boards:
        try:
            stored = stored_cycle_delay(board)
        except StoredCycleDelayError as e:
            if board is sbc:
                raise
            raise StoredCycleDelayError(
                f"{e} ({board.name!r} shares {sbc.name!r}'s power outlet)"
            ) from None
        if stored is None:
            continue
        source = (
            "SBC minimum"
            if board is sbc
            else f"minimum of {board.name!r} (on the same outlet)"
        )
        if best is None or stored > best[0]:
            best = (stored, source)
    return best or (DEFAULT_CYCLE_DELAY_SECONDS, "default minimum")


def board_cycle_delay(manager, sbc, requested=None) -> float:
    """The delay a power cycle of ``sbc`` waits between off and on, the
    same on every surface (CLI, MCP, REST, web).

    The floor is the largest stored delay among the boards on ``sbc``'s
    outlet, or :data:`DEFAULT_CYCLE_DELAY_SECONDS` when none is set
    (:func:`cycle_delay_floor`). With ``requested`` None the delay is the
    floor; otherwise ``requested`` raised to the floor. Raises ValueError
    for a ``requested`` value out of range (negative included), and
    :class:`StoredCycleDelayError` for an out-of-range stored one.
    """
    if requested is not None:
        validate_cycle_delay(requested)
    floor, _source = cycle_delay_floor(manager, sbc)
    if requested is None:
        return floor
    return max(float(requested), floor)


def check_power_on(manager, sbc) -> list:
    """Check the pre_power relays of ``sbc`` and of every board sharing
    its plug outlet; return those boards. Raises
    :class:`PrePowerCheckError` (naming a sharer) before anything is
    switched."""
    boards = boards_on_plug(manager, sbc)
    for board in boards:
        try:
            check_pre_power_actuators(manager, board)
        except PrePowerCheckError as e:
            if board is sbc:
                raise
            raise PrePowerCheckError(
                f"{e} ({board.name!r} shares {sbc.name!r}'s power outlet)"
            ) from e
    return boards


def _left_off(sbc, boards) -> str:
    others = [b.name for b in boards if b is not sbc]
    if not others:
        return f"{sbc.name!r} was left powered off"
    return (
        f"{sbc.name!r} and the boards sharing its outlet "
        f"({', '.join(repr(n) for n in others)}) were left powered off"
    )


def _apply_pre_power(manager, boards) -> None:
    """apply_pre_power_bindings for each board, with a busy channel
    (another verb in flight in this process) reported as an
    :class:`ActuationError` like any other strap failure."""
    try:
        for board in boards:
            apply_pre_power_bindings(manager, board)
    except ChannelBusyError as e:
        raise ActuationError(str(e)) from e


def _apply_after_power_off(manager, sbc, boards) -> None:
    """_apply_pre_power once the outlet is off: a failure says the boards
    were left off (they aren't powered on without their straps)."""
    try:
        _apply_pre_power(manager, boards)
    except ActuationError as e:
        raise ActuationError(f"{e}; {_left_off(sbc, boards)}") from e


def power_on_with_bindings(manager, sbc, controller) -> bool:
    """Drive the pre_power bindings of ``sbc`` (and of any board sharing
    its plug outlet), then power it on.

    Every path that powers a board on goes through here (or
    :func:`power_cycle_with_bindings`) so a strap is in its desired state
    whichever interface did it (issue #36). The relays are checked before
    any is driven; :class:`ActuationError` (a
    :class:`PrePowerCheckError` from the check) means power wasn't
    switched. Returns the controller's result otherwise.
    """
    boards = check_power_on(manager, sbc)
    _apply_pre_power(manager, boards)
    return controller.power_on()


def power_cycle_with_bindings(
    manager,
    sbc,
    controller,
    delay_s: Optional[float] = None,
    *,
    sleep_fn=None,
) -> bool:
    """Power-cycle ``sbc``: off, wait, drive pre_power bindings (its own
    and those of any board sharing its plug outlet), on.

    The wait is :func:`board_cycle_delay` of ``delay_s`` (a no-op for a
    value that came from it): the board's stored delay, or 3 s, is the
    minimum, and None means that minimum. Callers that report the delay
    compute it with :func:`board_cycle_delay` and pass the result.

    Before anything is switched: a bad ``delay_s`` or stored delay raises
    ValueError, and
    a pre_power relay that can't be opened or is busy raises
    :class:`PrePowerCheckError`, so the board keeps running. After
    power-off, a strap that can't be applied raises :class:`ActuationError`
    saying the board was left off (it is not powered on without its
    strap). A plug failure raises RuntimeError. Returns the result of
    power_on.
    """
    delay_s = board_cycle_delay(manager, sbc, delay_s)
    boards = check_power_on(manager, sbc)
    if not controller.power_off():
        raise RuntimeError(f"power_off failed on {sbc.name!r}")
    (sleep_fn or time.sleep)(delay_s)
    _apply_after_power_off(manager, sbc, boards)
    return controller.power_on()


def enter_recovery(
    manager,
    sbc,
    controller,
    *,
    delay_s: Optional[float] = None,
    sleep_fn=time.sleep,
) -> None:
    """Power-aware "enter USB Force Recovery (or equivalent)" composite.

    Sequence (see SPEC_actuators.md §"Composite, power-aware operations"):

      0. Pre-flight probe every actuator we'll touch — abort BEFORE
         power_off if any is unreachable.
      1. power_off
      2. sleep(board_cycle_delay(manager, sbc, delay_s)): the largest
         stored delay on its outlet, or 3 s, is the minimum (ValueError
         before step 0 if it or ``delay_s`` is out of range)
      3. actuate recovery_mode  → desired=asserted, strap engaged
      4. power_on (consults pre_power bindings; idempotent for the
         strap we just engaged)
    """
    # The board's stored delay is the minimum; an out-of-range one raises
    # ValueError before anything is touched.
    delay_s = board_cycle_delay(manager, sbc, delay_s)
    binding = manager.get_binding_by_target(sbc.id, "recovery_mode")
    if binding is None:
        raise ActuationError(f"no 'recovery_mode' binding on {sbc.name!r}")
    actuator, _channel = _resolve(manager, binding)

    # 0. Pre-flight probe. Abort before any power transition.
    outcome = probe_actuator(actuator)
    if outcome.result is not ProbeResult.OK:
        raise ActuationError(
            f"pre-flight probe failed on {actuator.name!r}: "
            f"{outcome.result.value} ({outcome.detail or 'no detail'})"
        )
    # The power-on step also drives the SBC's other pre_power bindings,
    # and those of boards sharing its outlet.
    boards = check_power_on(manager, sbc)

    # 1. Power off.
    if not controller.power_off():
        raise ActuationError(f"power_off failed on {sbc.name!r}")
    # 2. Settling delay.
    sleep_fn(delay_s)
    # 3. Engage recovery strap.
    actuate_binding(manager, binding)
    # 4. Power on (re-applies the strap via apply_pre_power_bindings).
    _apply_after_power_off(manager, sbc, boards)
    if not controller.power_on():
        raise ActuationError(f"power_on failed on {sbc.name!r}")


def exit_recovery(
    manager,
    sbc,
    controller,
    *,
    delay_s: Optional[float] = None,
    sleep_fn=time.sleep,
) -> None:
    """Power-aware "leave recovery" composite.

    Same shape as :func:`enter_recovery` but releases the strap.
    """
    # The board's stored delay is the minimum; an out-of-range one raises
    # ValueError before anything is touched.
    delay_s = board_cycle_delay(manager, sbc, delay_s)
    binding = manager.get_binding_by_target(sbc.id, "recovery_mode")
    if binding is None:
        raise ActuationError(f"no 'recovery_mode' binding on {sbc.name!r}")
    actuator, _channel = _resolve(manager, binding)

    outcome = probe_actuator(actuator)
    if outcome.result is not ProbeResult.OK:
        raise ActuationError(
            f"pre-flight probe failed on {actuator.name!r}: "
            f"{outcome.result.value} ({outcome.detail or 'no detail'})"
        )
    # The power-on step also drives the SBC's other pre_power bindings,
    # and those of boards sharing its outlet.
    boards = check_power_on(manager, sbc)

    if not controller.power_off():
        raise ActuationError(f"power_off failed on {sbc.name!r}")
    sleep_fn(delay_s)
    release_binding(manager, binding)
    _apply_after_power_off(manager, sbc, boards)
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
            manager,
            actuator,
            bindings_by_channel,
            held=held,
            drove=drove,
            failed=failed,
        )

    summary = {"held": held, "drove": drove, "failed": failed}
    _audit_safe_drive(manager, summary, ok=not failed)
    return summary


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
                    "safe-drive: leaving %s[%d] alone " "(binding %s desired=%s)",
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
