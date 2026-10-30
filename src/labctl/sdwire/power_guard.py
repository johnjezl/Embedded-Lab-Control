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


def board_off_problem_text(sbc_name: str, check: BoardOffCheck) -> str:
    """One sentence (no final period, may start lower-case) saying why
    ``check`` doesn't confirm ``sbc_name`` off; callers add their own
    framing and hints."""
    if check.problem == NO_PLUG:
        return f"{sbc_name} has no power plug, so labctl can't confirm it is off"
    if check.problem == UNREADABLE:
        return f"could not read the power state of {sbc_name}: {check.error}"
    if check.problem == POWERED_ON:
        return f"{sbc_name} is powered on"
    state = getattr(check.state, "value", check.state)
    return (
        f"the power state of {sbc_name} is {state}, so labctl can't confirm "
        "it is off"
    )


def power_off_before_host_switch(sbc) -> None:
    """Switch ``sbc`` off before its SD card goes to the host.

    Raises RuntimeError, before the card is touched, when the board can't
    be switched off: moving the card away from a running board risks bus
    contention and a corrupted card. A board with no power plug is left
    alone (labctl can't know or change its state).
    """
    if not sbc.power_plug:
        return
    from labctl.power import PowerController

    try:
        ok = PowerController.from_plug(sbc.power_plug).power_off()
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
    power_off_before_host_switch(sbc)
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
