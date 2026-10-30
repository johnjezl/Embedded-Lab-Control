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


def test_no_plug_ok_is_left_alone():
    """With no_plug_ok a board with no plug isn't touched (it has nothing
    to switch); without it, see TestNoPlugFailsClosed."""
    with patch("labctl.power.base.PowerController.from_plug") as from_plug:
        power_off_before_host_switch(
            SimpleNamespace(name="b", power_plug=None), no_plug_ok=True
        )
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


def test_missing_sdwire_leaves_the_board_running(board, power):
    ctrl, write = MagicMock(), MagicMock()
    ctrl.ensure_present.side_effect = RuntimeError("SDWire 'x' not found")
    with pytest.raises(RuntimeError, match="not found; nothing changed"):
        deploy_to_card(None, board, ctrl, write)
    power.power_off.assert_not_called()
    ctrl.switch_to_host.assert_not_called()


class TestCheckBoardOff:
    """The one "proceed only on OFF" check (#41 review)."""

    def _sbc(self, plug=True):
        sbc = MagicMock()
        sbc.power_plug = MagicMock() if plug else None
        return sbc

    def _check(self, sbc, state=None, error=None):
        from labctl.sdwire.power_guard import check_board_off

        power = MagicMock()
        if error:
            power.get_state.side_effect = error
        else:
            power.get_state.return_value = state
        with patch("labctl.power.PowerController.from_plug", return_value=power):
            return check_board_off(sbc)

    def test_off(self):
        from labctl.power.base import PowerState

        check = self._check(self._sbc(), PowerState.OFF)
        assert check.is_off and check.problem is None

    def test_no_plug(self):
        from labctl.sdwire import power_guard

        check = self._check(self._sbc(plug=False))
        assert check.problem == power_guard.NO_PLUG
        assert power_guard.board_off_problem_text("b", check) == (
            "b has no power plug, so labctl can't confirm it is off"
        )

    def test_unreadable(self):
        from labctl.sdwire import power_guard

        check = self._check(self._sbc(), error=RuntimeError("timeout"))
        assert check.problem == power_guard.UNREADABLE
        assert power_guard.board_off_problem_text("b", check) == (
            "could not read the power state of b: timeout"
        )

    def test_on(self):
        from labctl.power.base import PowerState
        from labctl.sdwire import power_guard

        check = self._check(self._sbc(), PowerState.ON)
        assert check.problem == power_guard.POWERED_ON
        assert power_guard.board_off_problem_text("b", check) == "b is powered on"

    def test_unknown(self):
        from labctl.power.base import PowerState
        from labctl.sdwire import power_guard

        check = self._check(self._sbc(), PowerState.UNKNOWN)
        assert check.problem == power_guard.NOT_OFF
        assert "power state of b is unknown" in power_guard.board_off_problem_text(
            "b", check
        )


class TestReadAccess:
    """The one read-access decision for the CLI and MCP read tools (#41
    review 3): on host -> second look -> power check."""

    @pytest.fixture(autouse=True)
    def no_sleep(self):
        with patch("labctl.sdwire.power_guard.time.sleep") as sleep:
            yield sleep

    def _ctrl(self, *on_host):
        ctrl = MagicMock()
        ctrl.card_on_host.side_effect = list(on_host)
        return ctrl

    def test_on_host_read_in_place(self, no_sleep):
        from labctl.sdwire.power_guard import read_access

        assert read_access(MagicMock(power_plug=None), self._ctrl(True)) == (
            False,
            None,
        )
        no_sleep.assert_not_called()

    def test_second_look_finds_it_on_host(self, no_sleep):
        from labctl.sdwire.power_guard import READ_RECHECK_SECONDS, read_access

        assert read_access(MagicMock(power_plug=None), self._ctrl(False, True)) == (
            False,
            None,
        )
        no_sleep.assert_called_once_with(READ_RECHECK_SECONDS)

    def test_on_board_without_plug_refused(self):
        from labctl.sdwire.power_guard import NO_PLUG, read_access

        needs_switch, check = read_access(
            MagicMock(power_plug=None), self._ctrl(False, False)
        )
        assert needs_switch is False and check.problem == NO_PLUG

    def test_force_switches_without_checking(self):
        from labctl.sdwire.power_guard import read_access

        with patch("labctl.sdwire.power_guard.check_board_off") as check:
            assert read_access(
                MagicMock(power_plug=None), self._ctrl(False, False), force=True
            ) == (True, None)
        check.assert_not_called()

    def test_board_off_switches(self):
        from labctl.power.base import PowerState
        from labctl.sdwire.power_guard import read_access

        power = MagicMock()
        power.get_state.return_value = PowerState.OFF
        with patch("labctl.power.PowerController.from_plug", return_value=power):
            assert read_access(MagicMock(), self._ctrl(False, False)) == (True, None)


def test_problem_text_never_recases_the_board_name():
    from labctl.sdwire.power_guard import (
        NO_PLUG,
        POWERED_ON,
        BoardOffCheck,
        board_off_problem_text,
    )

    for problem in (NO_PLUG, POWERED_ON):
        text = board_off_problem_text(
            "pi-lower", BoardOffCheck(problem), sentence_start=True
        )
        assert text.startswith("pi-lower ")


class TestNoPlugFailsClosed:
    """A board with no plug can't be switched off before its card is
    taken: refused unless no_plug_ok (#41 review 4)."""

    def test_no_plug_refused(self):
        from labctl.sdwire.power_guard import NoPlugError

        with pytest.raises(NoPlugError, match="'b' has no power plug"):
            power_off_before_host_switch(SimpleNamespace(name="b", power_plug=None))

    def test_no_plug_ok(self):
        power_off_before_host_switch(
            SimpleNamespace(name="b", power_plug=None), no_plug_ok=True
        )

    def test_no_plug_ok_doesnt_skip_or_excuse_a_plug(self, board):
        power = MagicMock()
        power.power_off.return_value = False
        with (
            patch("labctl.power.PowerController.from_plug", return_value=power),
            pytest.raises(RuntimeError, match="could not power off 'board'"),
        ):
            power_off_before_host_switch(board, no_plug_ok=True)
        power.power_off.assert_called_once()

    def test_deploy_to_card_refuses_no_plug(self):
        ctrl = MagicMock()
        with pytest.raises(RuntimeError, match="has no power plug"):
            deploy_to_card(
                MagicMock(),
                SimpleNamespace(name="b", power_plug=None),
                ctrl,
                MagicMock(),
            )
        ctrl.switch_to_host.assert_not_called()
