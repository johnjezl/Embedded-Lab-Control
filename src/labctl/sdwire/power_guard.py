"""Power a board off before its SD card is switched to the host, and
check that it is off before the card is taken from it."""

import time
from dataclasses import dataclass
from typing import Optional

# Why a board isn't confirmed off (BoardOffCheck.problem).
NO_PLUG = "no_plug"
UNREADABLE = "unreadable"
POWERED_ON = "powered_on"
NOT_OFF = "not_off"  # a state other than ON or OFF (e.g. unknown)


@dataclass
class BoardOffCheck:
    """Result of :func:`check_board_off`: ``problem`` is None when the
    plug reports the board OFF, else one of NO_PLUG, UNREADABLE (``error``
    says why), POWERED_ON or NOT_OFF (``state`` says which)."""

    problem: Optional[str] = None
    state: object = None  # a PowerState when the plug was read
    error: Optional[str] = None

    @property
    def is_off(self) -> bool:
        return self.problem is None


def check_board_off(sbc) -> BoardOffCheck:
    """Whether ``sbc``'s plug reports it OFF: the one check behind every
    "proceed only when the board is off" refusal (``sdwire host`` and
    MCP ``sdwire_to_host``/``sdwire_ls``/``cat``/``info``, and the CLI
    ``sdwire ls``/``cat``/``info``). Fails closed: no plug, a plug that
    can't be read, or any state but OFF is a problem. Each caller words
    its own refusal."""
    if not sbc.power_plug:
        return BoardOffCheck(NO_PLUG)
    from labctl.power import PowerController
    from labctl.power.base import PowerState

    try:
        state = PowerController.from_plug(sbc.power_plug).get_state()
    except Exception as e:
        return BoardOffCheck(UNREADABLE, error=str(e))
    if state == PowerState.OFF:
        return BoardOffCheck(state=state)
    if state == PowerState.ON:
        return BoardOffCheck(POWERED_ON, state=state)
    return BoardOffCheck(NOT_OFF, state=state)


def board_off_problem_text(
    sbc_name: str, check: BoardOffCheck, *, sentence_start: bool = False
) -> str:
    """One sentence (no final period) saying why ``check`` doesn't confirm
    ``sbc_name`` off; callers add their own framing and hints. Generic
    leading words start lower-case unless ``sentence_start``; the board
    name is never re-cased."""

    def generic(text: str) -> str:
        return text[0].upper() + text[1:] if sentence_start else text

    if check.problem == NO_PLUG:
        return f"{sbc_name} has no power plug, so labctl can't confirm it is off"
    if check.problem == UNREADABLE:
        return generic(f"could not read the power state of {sbc_name}: {check.error}")
    if check.problem == POWERED_ON:
        return f"{sbc_name} is powered on"
    state = getattr(check.state, "value", check.state)
    return generic(
        f"the power state of {sbc_name} is {state}, so labctl can't confirm "
        "it is off"
    )


# Second look before treating the card as on the board (see read_access).
READ_RECHECK_SECONDS = 1.0


def read_access(sbc, ctrl, *, force: bool = False):
    """How a read of the SD card (CLI ``sdwire ls``/``cat``/``info``, the
    MCP read tools) reaches it. Returns ``(needs_switch, refusal)``:

    - Card already on the host (``ctrl.card_on_host()`` probes the reader
      for media): read it in place and leave the mux alone; the board isn't
      using the card, so its power doesn't matter. ``(False, None)``.
    - Card on the board: switch to the host and back only when
      :func:`check_board_off` confirms it off (or ``force``):
      ``(True, None)``; otherwise ``(False, check)`` with the failed
      :class:`BoardOffCheck` for the caller to word.

    A card switched to the host moments ago (by another process) may still
    be initialising and probe as "no medium"; concluding "on the board"
    would make the cleanup switch it there, so it is probed once more
    after :data:`READ_RECHECK_SECONDS`. (The mux can't be read back.)
    """
    if ctrl.card_on_host():
        return False, None
    time.sleep(READ_RECHECK_SECONDS)
    if ctrl.card_on_host():
        return False, None
    if force:
        return True, None
    check = check_board_off(sbc)
    if not check.is_off:
        return False, check
    return True, None


class NoPlugError(RuntimeError):
    """The board has no power plug, so it can't be switched off (or known
    to be off) before its SD card is taken; callers add how to proceed."""


def no_plug_refusal(sbc) -> str:
    return (
        f"{sbc.name!r} has no power plug, so labctl can't switch it off or "
        "confirm it is off before switching its SD card to the host; nothing "
        "was written to the card"
    )


def power_off_before_host_switch(manager, sbc, *, no_plug_ok: bool = False) -> None:
    """Switch ``sbc`` off before its SD card goes to the host.

    Raises RuntimeError, before the card is touched, when the board can't
    be switched off: moving the card away from a running board risks bus
    contention and a corrupted card. A board with no power plug fails
    closed too (:class:`NoPlugError`), unless ``no_plug_ok``: the caller
    has made sure it is off. ``no_plug_ok`` never skips a power-off that a
    plug makes possible, nor excuses one that fails.
    """
    if not sbc.power_plug:
        if no_plug_ok:
            return
        raise NoPlugError(no_plug_refusal(sbc))
    from labctl.actuators.runtime import power_off_board
    from labctl.power import PowerController

    try:
        ok = power_off_board(manager, sbc, PowerController.from_plug(sbc.power_plug))
    except Exception as e:
        ok, why = False, f": {e}"
    else:
        why = ""
    if not ok:
        raise RuntimeError(
            f"could not power off {sbc.name!r} before switching its SD card "
            f"to the host{why}; nothing was written to the card"
        )
    time.sleep(1)


def deploy_to_card(manager, sbc, ctrl, write) -> None:
    """Power ``sbc`` off, switch its card to the host, run ``write()``, and
    hand the card back to the board.

    Raises RuntimeError for any failure. Once the board is off, the error
    says it was left off, and whether the card is still on the host.
    """
    from labctl.actuators.runtime import _left_off, boards_on_plug

    # A missing SDWire must not cost a running board its power.
    try:
        ctrl.ensure_present()
    except Exception as e:
        raise RuntimeError(f"{e}; nothing changed") from e
    power_off_before_host_switch(manager, sbc)
    try:
        ctrl.switch_to_host()
        time.sleep(2)
        write()
    except Exception as e:
        try:
            ctrl.switch_to_dut()
            card = ""
        except Exception:
            card = " (its SD card may still be on the host)"
        raise RuntimeError(
            f"deploy to {sbc.name!r} failed: {e}; "
            f"{_left_off(sbc, boards_on_plug(manager, sbc))}{card}"
        ) from e
    try:
        ctrl.switch_to_dut()
    except Exception as e:
        raise RuntimeError(
            f"deployed to {sbc.name!r}, but its SD card could not be switched "
            f"back to the board: {e}; the card is still on the host and "
            f"{_left_off(sbc, boards_on_plug(manager, sbc))}"
        ) from e
