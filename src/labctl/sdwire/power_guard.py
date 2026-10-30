"""Power a board off before its SD card is switched to the host."""

import time


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
