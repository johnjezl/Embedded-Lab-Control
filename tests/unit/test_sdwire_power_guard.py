"""labctl.sdwire.power_guard: the SD card never leaves a running board."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from labctl.sdwire.power_guard import deploy_to_card, power_off_before_host_switch


@pytest.fixture
def board():
    return SimpleNamespace(name="board", power_plug=object())


@pytest.fixture
def power():
    controller = MagicMock()
    controller.power_off.return_value = True
    with (
        patch("labctl.power.base.PowerController.from_plug", return_value=controller),
        patch("labctl.sdwire.power_guard.time.sleep"),
        patch("labctl.actuators.runtime.boards_on_plug", lambda m, sbc: [sbc]),
    ):
        yield controller


def test_no_plug_is_left_alone():
    with patch("labctl.power.base.PowerController.from_plug") as from_plug:
        power_off_before_host_switch(SimpleNamespace(name="b", power_plug=None))
    from_plug.assert_not_called()


@pytest.mark.parametrize("failure", ["false", "raises"])
def test_power_off_failure_touches_nothing(board, power, failure):
    if failure == "false":
        power.power_off.return_value = False
    else:
        power.power_off.side_effect = OSError("plug unreachable")
    ctrl, write = MagicMock(), MagicMock()
    with pytest.raises(RuntimeError, match="nothing was written to the card"):
        deploy_to_card(None, board, ctrl, write)
    ctrl.switch_to_host.assert_not_called()
    write.assert_not_called()


def test_success_hands_the_card_back(board, power):
    calls = []
    ctrl = MagicMock()
    ctrl.switch_to_host.side_effect = lambda: calls.append("host")
    ctrl.switch_to_dut.side_effect = lambda: calls.append("dut")
    deploy_to_card(None, board, ctrl, lambda: calls.append("write"))
    assert calls == ["host", "write", "dut"]
    power.power_off.assert_called_once()


def test_write_failure_hands_the_card_back(board, power):
    ctrl = MagicMock()
    with pytest.raises(RuntimeError) as exc:
        deploy_to_card(None, board, ctrl, MagicMock(side_effect=OSError("mount")))
    assert str(exc.value) == (
        "deploy to 'board' failed: mount; 'board' was left powered off"
    )
    ctrl.switch_to_dut.assert_called_once()


def test_write_failure_and_hand_back_failure(board, power):
    ctrl = MagicMock()
    ctrl.switch_to_dut.side_effect = OSError("usb gone")
    with pytest.raises(RuntimeError, match="may still be on the host"):
        deploy_to_card(None, board, ctrl, MagicMock(side_effect=OSError("mount")))


def test_hand_back_failure_after_a_good_write_says_it_was_deployed(board, power):
    ctrl = MagicMock()
    ctrl.switch_to_dut.side_effect = OSError("usb gone")
    write = MagicMock()
    with pytest.raises(RuntimeError) as exc:
        deploy_to_card(None, board, ctrl, write)
    write.assert_called_once()
    assert str(exc.value).startswith(
        "deployed to 'board', but its SD card could not be switched back"
    )
    assert "the card is still on the host" in str(exc.value)
    assert "'board' was left powered off" in str(exc.value)
