"""Hardware actions are recorded in the activity stream, once each, by the
shared code every front end goes through (#57 part 2)."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from labctl.core import audit
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import AddressType, PlugType

SRC = Path(__file__).resolve().parents[2] / "src" / "labctl"


@pytest.fixture(autouse=True)
def reset_audit_context():
    audit.set_context("internal", "internal")
    yield
    audit.set_context("internal", "internal")


@pytest.fixture
def manager(tmp_path):
    return ResourceManager(get_database(tmp_path / "test.db"))


@pytest.fixture
def board(manager):
    sbc = manager.create_sbc(name="pi")
    manager.assign_power_plug(sbc.id, plug_type=PlugType.TASMOTA, address="10.9.9.9")
    sw = manager.create_sdwire_device(name="sw", serial_number="sn-1")
    manager.assign_sdwire(sbc.id, sw.id)
    return manager.get_sbc_by_name("pi")


def _events(manager, *actions):
    rows = manager.db.execute("SELECT * FROM audit_log ORDER BY id")
    return [dict(r) for r in rows if not actions or r["action"] in actions]


def _details(event):
    return json.loads(event["details"]) if event["details"] else {}


class TestTarget:
    def test_ok_error_false_and_summary(self, manager, board):
        t = audit.Target(manager.db, board)
        assert t.run("a", lambda: {"n": 2}, summary=lambda r: r, x=1) == {"n": 2}
        assert t.run("b", lambda: False) is False
        with pytest.raises(OSError):
            t.run("c", lambda: (_ for _ in ()).throw(OSError("gone")))
        events = _events(manager, "a", "b", "c")
        assert [(e["action"], e["result"]) for e in events] == [
            ("a", "ok"),
            ("b", "error"),
            ("c", "error"),
        ]
        assert _details(events[0]) == {"x": 1, "n": 2}
        assert _details(events[2]) == {"error": "gone"}
        assert {e["entity_name"] for e in events} == {"pi"}

    def test_without_a_database_records_nothing(self):
        t = audit.Target(None, SimpleNamespace(name="x"))
        assert t.run("a", lambda: 5) == 5


class TestPower:
    def _controller(self, ok=True):
        c = MagicMock()
        c.power_on.return_value = ok
        c.power_off.return_value = ok
        return c

    def test_on_off_cycle(self, manager, board):
        from labctl.actuators.runtime import (
            power_cycle_with_bindings,
            power_off_board,
            power_on_with_bindings,
        )

        c = self._controller()
        with audit.activity_context("mcp-stdio:1-2", "mcp", agent="bot"):
            power_on_with_bindings(manager, board, c)
            power_off_board(manager, board, c)
            power_cycle_with_bindings(manager, board, c, 5, sleep_fn=lambda s: None)
        events = _events(manager, "power_on", "power_off", "power_cycle")
        assert [(e["action"], e["result"], e["agent"]) for e in events] == [
            ("power_on", "ok", "bot"),
            ("power_off", "ok", "bot"),
            ("power_cycle", "ok", "bot"),
        ]
        assert _details(events[2]) == {"delay_seconds": 5.0}

    def test_failures_recorded(self, manager, board):
        from labctl.actuators.runtime import power_cycle_with_bindings, power_off_board

        assert power_off_board(manager, board, self._controller(ok=False)) is False
        c = self._controller()
        c.power_off.side_effect = OSError("plug unreachable")
        with pytest.raises(OSError):
            power_cycle_with_bindings(manager, board, c, 5, sleep_fn=lambda s: None)
        with pytest.raises(ValueError):  # refused input: nothing switched
            power_cycle_with_bindings(manager, board, c, -5, sleep_fn=lambda s: None)
        events = _events(manager, "power_off", "power_cycle")
        assert [(e["action"], e["result"]) for e in events] == [
            ("power_off", "error"),
            ("power_cycle", "error"),
        ]
        assert "plug unreachable" in _details(events[1])["error"]

    def test_recovery_recorded_once(self, manager, board):
        from labctl.actuators.runtime import ActuationError, enter_recovery

        with pytest.raises(ActuationError):  # no recovery_mode binding
            enter_recovery(manager, board, self._controller(), sleep_fn=lambda s: None)
        events = _events(manager, "enter_recovery")
        assert [(e["result"]) for e in events] == ["error"]
        assert "recovery_mode" in _details(events[0])["error"]


class TestRobustness:
    def test_interrupt_is_recorded(self, manager, board):
        from labctl.actuators.runtime import power_cycle_with_bindings

        c = MagicMock()
        c.power_off.return_value = True

        def ctrl_c(_seconds):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            power_cycle_with_bindings(manager, board, c, 5, sleep_fn=ctrl_c)
        (event,) = _events(manager, "power_cycle")
        assert event["result"] == "interrupted"

    def test_a_failing_summary_never_fails_the_action(self, manager, board):
        t = audit.Target(manager.db, board)
        assert t.run("a", lambda: None, summary=lambda r: r["x"]) is None
        assert t.run("b", lambda: {"action": 1, "result": 2}, summary=dict)
        assert [e["result"] for e in _events(manager, "a", "b")] == ["ok", "ok"]

    def test_refusal_before_switching_leaves_sharers_alone(self, manager, board):
        from labctl.actuators.runtime import ActuationError, enter_recovery

        other = manager.create_sbc(name="pi-b")
        manager.assign_power_plug(
            other.id, plug_type=PlugType.TASMOTA, address="10.9.9.9"
        )
        with pytest.raises(ActuationError):  # no recovery_mode binding
            enter_recovery(manager, board, MagicMock(), sleep_fn=lambda s: None)
        assert [e["entity_name"] for e in _events(manager, "enter_recovery")] == ["pi"]
        assert _events(manager, "power_cycle") == []

    def test_power_all_records_each_board_once(self, manager, board):
        from labctl.actuators.runtime import power_off_board

        other = manager.create_sbc(name="pi-b")
        manager.assign_power_plug(
            other.id, plug_type=PlugType.TASMOTA, address="10.9.9.9"
        )
        c = MagicMock()
        c.power_off.return_value = True
        power_off_board(manager, board, c, skip_sharers={"pi-b"})
        power_off_board(manager, manager.get_sbc_by_name("pi-b"), c)
        names = [e["entity_name"] for e in _events(manager, "power_off")]
        assert names == ["pi", "pi-b", "pi"]  # pi-b's switch reaches pi too

    def test_power_all_project_filter_records_the_board_left_out(self, tmp_path):
        """power-all --project pa switches b too (shared outlet): b gets an
        event though it isn't one of the targets."""
        from click.testing import CliRunner

        from labctl.cli import main

        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        m = ResourceManager(get_database(tmp_path / "labctl.db"))
        for name, project in (("a", "pa"), ("b", "pb")):
            sbc = m.create_sbc(name=name, project=project)
            m.assign_power_plug(sbc.id, plug_type=PlugType.TASMOTA, address="10.0.0.1")
        plug = MagicMock()
        plug.power_on.return_value = True
        with patch("labctl.power.base.PowerController.from_plug", return_value=plug):
            result = CliRunner().invoke(
                main,
                ["-c", str(config), "power-all", "on", "--project", "pa", "--yes"],
            )
        assert result.exit_code == 0, result.output
        events = [
            (e["entity_name"], _details(e).get("via")) for e in _events(m, "power_on")
        ]
        assert events == [("a", None), ("b", "a")]

    def test_boards_sharing_the_outlet_get_the_event(self, manager, board):
        from labctl.actuators.runtime import power_cycle_with_bindings

        other = manager.create_sbc(name="pi-b")
        manager.assign_power_plug(
            other.id, plug_type=PlugType.TASMOTA, address="10.9.9.9"
        )
        c = MagicMock()
        c.power_off.return_value = c.power_on.return_value = True
        power_cycle_with_bindings(manager, board, c, 5, sleep_fn=lambda s: None)
        events = _events(manager, "power_cycle")
        assert [(e["entity_name"], _details(e).get("via")) for e in events] == [
            ("pi", None),
            ("pi-b", "pi"),
        ]


class TestSdwire:
    def test_bound_controller_records(self, manager, board):
        from labctl.sdwire.controller import SDWireController

        ctrl = SDWireController("sn-1", sbc=board, db=manager.db)
        device = MagicMock()
        with (
            patch.object(SDWireController, "_get_device", return_value=device),
            patch.object(
                SDWireController,
                "_flash_image",
                return_value={"bytes_written": 42, "elapsed_seconds": 1},
            ),
            patch.object(
                SDWireController,
                "_update_files",
                return_value={"copied": ["/boot/Image"], "renamed": [], "deleted": []},
            ),
        ):
            ctrl.switch_to_host()
            ctrl.flash_image("/srv/images/os.img.xz")
            ctrl.update_files(1, [("/tmp/Image", "/boot/Image")])
            ctrl.switch_to_dut()
        events = _events(manager)
        assert [e["action"] for e in events][-4:] == [
            "sdwire_to_host",
            "flash_image",
            "sdwire_update",
            "sdwire_to_dut",
        ]
        flash, update = events[-3], events[-2]
        assert _details(flash) == {"image": "os.img.xz", "bytes_written": 42}
        assert _details(update)["copied"] == ["/boot/Image"]
        assert _details(update)["partition"] == 1

    def test_switch_failure_recorded(self, manager, board):
        from labctl.sdwire.controller import SDWireController

        ctrl = SDWireController("sn-1", sbc=board, db=manager.db)
        with patch.object(
            SDWireController, "_get_device", side_effect=RuntimeError("not found")
        ):
            with pytest.raises(RuntimeError):
                ctrl.switch_to_host()
        (event,) = _events(manager, "sdwire_to_host")
        assert event["result"] == "error"

    def test_unbound_controller_records_nothing(self, manager, board):
        from labctl.sdwire.controller import SDWireController

        before = len(_events(manager))
        with patch.object(SDWireController, "_get_device", return_value=MagicMock()):
            SDWireController("sn-1").switch_to_dut()
        assert len(_events(manager)) == before


class TestSerialAndBootTest:
    def test_send_records_size_never_data(self, manager, board):
        from labctl.serial.capture import SendResult, send_serial_data

        with patch(
            "labctl.serial.capture._send_serial_data",
            return_value=SendResult(sent=True, bytes_sent=9),
        ):
            send_serial_data(
                "localhost", 4000, "hunter2\n", target=audit.Target(manager.db, board)
            )
        (event,) = _events(manager, "serial_send")
        assert _details(event) == {"bytes_sent": 9}
        assert "hunter2" not in (event["details"] or "")

    def test_capture_is_a_read_and_not_recorded(self):
        import inspect

        from labctl.serial.capture import capture_serial_output

        assert "target" not in inspect.signature(capture_serial_output).parameters

    def test_port_target_is_the_board(self, manager, board):
        from labctl.serial.capture import port_target

        port = SimpleNamespace(sbc_id=board.id, id=7, alias="c", device_path="/d")
        t = port_target(manager, port)
        assert (t.sbc_name, t.defaults) == ("pi", {"port": "c"})
        t.record("serial_send", bytes_sent=3)
        (event,) = _events(manager, "serial_send")
        assert _details(event) == {"port": "c", "bytes_sent": 3}
        orphan = SimpleNamespace(sbc_id=0, id=7, alias="c", device_path="/d")
        t = port_target(manager, orphan)
        assert (t.entity_type, t.sbc_name) == ("serial_port", "c")

    def test_boot_test_recorded_with_counts(self, manager, board):
        from labctl.serial.boot_test import BootRunResult, BootTestResult, run_boot_test

        result = BootTestResult(
            sbc_name="pi", expect_pattern="login:", total_runs=2, timeout_per_run=5
        )
        result.runs = [
            BootRunResult(1, True, 1.0, True),
            BootRunResult(2, False, 5.0, False),
        ]
        with patch("labctl.serial.boot_test._run_boot_test", return_value=result):
            run_boot_test(
                "pi",
                "login:",
                "localhost",
                4000,
                power_cycle_fn=lambda: None,
                runs=2,
                image="/srv/os.img",
                target=audit.Target(manager.db, board),
            )
        (event,) = _events(manager, "boot_test")
        assert _details(event) == {
            "runs": 2,
            "image": "os.img",
            "completed_runs": 2,
            "passed": 1,
        }


class TestBootTestOutcome:
    def test_stopped_test_is_an_error(self, manager, board):
        from labctl.serial.boot_test import BootTestResult, run_boot_test

        result = BootTestResult(
            sbc_name="pi", expect_pattern="x", total_runs=3, timeout_per_run=5
        )
        result.stopped_reason = "claim lost"
        with patch("labctl.serial.boot_test._run_boot_test", return_value=result):
            run_boot_test(
                "pi",
                "x",
                "localhost",
                4000,
                power_cycle_fn=lambda: None,
                runs=3,
                target=audit.Target(manager.db, board),
            )
        (event,) = _events(manager, "boot_test")
        assert event["result"] == "error"
        assert _details(event)["stopped"] == "claim lost"


def test_claim_renew_and_release_request_carry_the_claim(manager, board):
    claim = manager.claim_sbc(
        sbc_name="pi",
        agent_name="a",
        session_id="s",
        session_kind="cli",
        duration_seconds=60,
        reason="r",
    )
    manager.renew_claim("pi", "s", duration_seconds=120)
    manager.record_release_request("pi", "b", "need it")
    events = _events(manager, "claim_renew", "release_request")
    assert [_details(e)["claim"] for e in events] == [claim.id, claim.id]


class TestFrontEndsPassTheBoard:
    """Structural: the front ends use the recording paths, so a new call
    site can't silently skip the activity stream."""

    @staticmethod
    def _calls(path, name):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if getattr(f, "id", None) == name or getattr(f, "attr", None) == name:
                    yield node

    # Reads: their host round trip only looks at the card, so they record
    # nothing (spec: reads are not recorded).
    READS = {
        "sdwire_ls",
        "sdwire_cat",
        "sdwire_info",
        "sdwire_ls_cmd",
        "sdwire_cat_cmd",
        "sdwire_info_cmd",
    }

    def test_every_writing_sdwire_controller_is_bound_to_its_board(self):
        found = 0
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for call in ast.walk(fn):
                    if not (
                        isinstance(call, ast.Call)
                        and getattr(call.func, "id", None) == "SDWireController"
                    ):
                        continue
                    found += 1
                    kws = {k.arg for k in call.keywords}
                    if fn.name in self.READS:
                        assert not {"sbc", "db"} & kws, f"{fn.name} records a read"
                    else:
                        assert {"sbc", "db"} <= kws, f"{path.name}:{call.lineno}"
        assert found >= 16

    @pytest.mark.parametrize("name", ["send_serial_data", "run_boot_test"])
    def test_serial_and_boot_test_calls_pass_a_target(self, name):
        for path in (SRC / "cli.py", SRC / "mcp_server.py"):
            calls = list(self._calls(path, name))
            assert calls, f"{name} not called in {path.name}"
            for call in calls:
                assert "target" in {
                    k.arg for k in call.keywords
                }, f"{path.name}:{call.lineno}"

    def test_no_front_end_powers_off_directly(self):
        allowed = {SRC / "actuators" / "runtime.py"} | set(
            (SRC / "power").rglob("*.py")
        )
        for path in SRC.rglob("*.py"):
            if path in allowed:
                continue
            for call in self._calls(path, "power_off"):
                assert not isinstance(call.func, ast.Attribute) or (
                    getattr(call.func.value, "id", None) == "runtime"
                ), f"{path.relative_to(SRC)}:{call.lineno} calls .power_off()"


class TestMcpWiring:
    """Through the real MCP tools: the event is recorded once, as mcp."""

    @pytest.fixture
    def mcp_env(self, manager, board):
        pytest.importorskip("mcp")
        with (
            patch("labctl.mcp_server._get_manager", return_value=manager),
            patch("labctl.mcp_server._confirmation_required", return_value=False),
            patch("labctl.mcp_server._SESSION_ID", "mcp-stdio:5-5"),
            patch("labctl.mcp_server._SESSION_KIND", "mcp-stdio"),
            patch("labctl.mcp_server._AGENT_NAME", "fw-bot"),
        ):
            yield manager

    def test_power_off_and_sdwire_to_dut(self, mcp_env):
        from labctl import mcp_server
        from labctl.sdwire.controller import SDWireController

        plug = MagicMock()
        plug.power_off.return_value = True
        with (
            patch("labctl.power.base.PowerController.from_plug", return_value=plug),
            patch.object(SDWireController, "_get_device", return_value=MagicMock()),
        ):
            assert "Power OFF" in mcp_server.power_off("pi")
            assert "DUT" in mcp_server.sdwire_to_dut("pi")
        events = _events(mcp_env, "power_off", "sdwire_to_dut")
        assert [(e["action"], e["source"], e["agent"]) for e in events] == [
            ("power_off", "mcp", "fw-bot"),
            ("sdwire_to_dut", "mcp", "fw-bot"),
        ]


# Every MCP tool that changes something, and what it records when it does.
# A new tool fails test_every_mutating_tool_is_mapped until it is added here
# (and records what it should).
RECORDED_AS = {
    "power_on": {"power_on"},
    "power_off": {"power_off"},
    "power_cycle": {"power_cycle"},
    "run_health_check": set(),  # observes only; status changes aren't actions
    "add_sbc": {"create"},
    "remove_sbc": {"delete"},
    "update_sbc": {"update"},
    "assign_serial_port": {"assign"},
    "assign_power_plug": {"assign"},
    "set_network_address": {"set"},
    "remove_serial_port": {"remove"},
    "remove_network_address": {"remove"},
    "remove_power_plug": {"remove"},
    "add_serial_device": {"create"},
    "remove_serial_device": {"delete"},
    "sdwire_add": {"create"},
    "sdwire_remove": {"delete"},
    "sdwire_assign": {"assign"},
    "sdwire_unassign": {"remove"},
    "sdwire_to_dut": {"sdwire_to_dut"},
    "sdwire_to_host": {"sdwire_to_host"},
    "sdwire_update": {"power_off", "sdwire_to_host", "sdwire_update"},
    "flash_image": {"power_off", "sdwire_to_host", "flash_image"},
    "serial_send": {"serial_send"},
    "boot_test": {"boot_test", "power_cycle"},
    "claim_sbc": {"claim"},
    "release_sbc": {"release"},
    "renew_sbc_claim": {"claim_renew"},
    "request_sbc_release": {"release_request"},
    "force_release_sbc": {"force_release"},
    "actuator_add": {"create"},
    "actuator_remove": {"delete"},
    "actuator_set": {"actuator_set"},
    "bind": {"create"},
    "unbind": {"delete"},
    "actuate": {"actuate"},
    "release": {"release"},
    "press": {"press"},
    "delete_image": {"image_delete"},
    "enter_recovery": {"enter_recovery"},
    "exit_recovery": {"exit_recovery"},
}


def test_every_mutating_tool_is_mapped():
    pytest.importorskip("mcp")
    from labctl.mcp_server import _TOOL_ANNOTATION_TABLE

    # (title, read_only, destructive, idempotent, open_world)
    mutating = {name for name, a in _TOOL_ANNOTATION_TABLE.items() if not a[1]}
    assert mutating == set(RECORDED_AS)


def test_removals_are_recorded(manager, board):
    manager.set_network_address(
        board.id, address_type=AddressType.ETHERNET, ip_address="10.0.0.5"
    )
    assert manager.remove_network_address(board.id, AddressType.ETHERNET)
    assert manager.remove_power_plug(board.id)
    assert manager.unassign_sdwire(board.id)
    assert not manager.unassign_sdwire(board.id)  # nothing left: no event
    removed = [(e["entity_type"], e["entity_name"]) for e in _events(manager, "remove")]
    assert removed == [
        ("network_address", "pi"),
        ("power_plug", "pi"),
        ("sdwire", "pi"),
    ]


def test_mapped_actions_exist_in_the_code():
    """Every action named above is one the code can record."""
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for actions in RECORDED_AS.values():
        for action in actions:
            assert f'"{action}"' in text, action
