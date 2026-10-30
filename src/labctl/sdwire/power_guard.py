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


def deploy_to_card(sbc, ctrl, write) -> None:
    """Power ``sbc`` off, switch its card to the host, run ``write()``, and
    hand the card back to the board.

    Raises RuntimeError for any failure. Once the board is off, the error
    says it was left off (the card is handed back to the board first, as
    far as that still works).
    """
    power_off_before_host_switch(sbc)
    try:
        ctrl.switch_to_host()
        time.sleep(2)
        write()
        ctrl.switch_to_dut()
    except Exception as e:
        try:
            ctrl.switch_to_dut()
        except Exception:
            pass
        raise RuntimeError(
            f"deploy to {sbc.name!r} failed: {e}; the board (and any board "
            f"sharing its outlet) was left powered off"
        ) from e
