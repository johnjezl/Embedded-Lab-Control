"""Claim refusals are recorded once, where a call is refused, and checks
that only look record nothing (#60)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.core import audit
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import ClaimActor, PlugType


@pytest.fixture(autouse=True)
def reset_audit_context():
    audit.set_context("internal", "internal")
    yield
    audit.set_context("internal", "internal")


@pytest.fixture
def manager(tmp_path):
    m = ResourceManager(get_database(tmp_path / "labctl.db"))
    for name in ("a", "b"):
        sbc = m.create_sbc(name=name)
        m.assign_power_plug(
            sbc.id, plug_type=PlugType.TASMOTA, address=f"10.0.0.{sbc.id}"
        )
    return m


def _hold(manager, sbc="a", session="other-session"):
    return manager.claim_sbc(
        sbc_name=sbc,
        agent_name="holder",
        session_id=session,
        session_kind="cli",
        duration_seconds=600,
        reason="mine",
    )


def _refusals(manager):
    rows = manager.db.execute(
        "SELECT * FROM audit_log WHERE action = 'claim_refused' ORDER BY id"
    )
    return [dict(r, details=json.loads(r["details"])) for r in rows]


def test_record_claim_refusal(manager):
    claim = _hold(manager, "b")
    manager.record_claim_refusal("a", "claimed", claim=claim, attempted="power_on")
    (event,) = _refusals(manager)
    assert (event["entity_name"], event["result"]) == ("a", "forbidden")
    assert event["details"] == {
        "reason": "claimed",
        "attempted": "power_on",
        "holder": "holder",
        "claim": claim.id,
        "held_board": "b",
    }
    assert event["claim_id"] is None  # never the foreign key (#58)


def test_a_failed_lookup_never_breaks_the_refusal(manager):
    with patch.object(manager.db, "execute_one", side_effect=OSError("locked")):
        manager.record_claim_refusal("a", "claimed")  # no exception
    (event,) = _refusals(manager)
    assert (event["entity_name"], event["entity_id"]) == ("a", None)


def test_force_release_of_nothing_is_not_a_refusal(manager):
    manager.record_override_refusal("a", "force_release_sbc")  # unclaimed
    manager.record_override_refusal("typo", "force_release_sbc")  # unknown
    assert _refusals(manager) == []


def test_keepalive_recheck_records_nothing(manager):
    from labctl.core.claim_keepalive import ClaimKeepalive

    _hold(manager, "a")
    keepalive = ClaimKeepalive(manager, "a", ClaimActor("me"), interval=3600)
    assert keepalive._recheck() is not None  # stops the operation
    assert _refusals(manager) == []


class TestMcp:
    @pytest.fixture
    def mcp(self, manager):
        pytest.importorskip("mcp")
        from labctl import mcp_server
        from labctl.core.config import Config

        config = Config()
        with (
            patch("labctl.mcp_server._get_manager", return_value=manager),
            patch("labctl.mcp_server._get_config", return_value=config),
            patch("labctl.mcp_server._SESSION_ID", "mcp-stdio:7-7"),
            patch("labctl.mcp_server._SESSION_KIND", "mcp-stdio"),
            patch("labctl.mcp_server._AGENT_NAME", "bot"),
        ):
            yield mcp_server, config

    def test_refused_tool_recorded_once_with_tool_name(self, mcp, manager):
        mcp_server, _config = mcp
        _hold(manager, "a")
        with patch("labctl.mcp_server._confirmation_required", return_value=False):
            result = json.loads(mcp_server.power_on("a"))
        assert result["error"] == "sbc_claimed"
        (event,) = _refusals(manager)
        assert (event["actor"], event["agent"]) == ("mcp-stdio:7-7", "bot")
        assert event["details"]["attempted"] == "power_on"

    def test_refusal_while_planning_is_the_callers(self, mcp, manager):
        """A destructive tool's first call (the plan) refuses outside the
        hardware lock; it is still attributed to the caller."""
        mcp_server, _config = mcp
        _hold(manager, "a")
        with patch("labctl.mcp_server._confirmation_required", return_value=True):
            result = mcp_server.power_cycle("a")
        assert "confirm_token" not in result  # refused while planning
        (event,) = _refusals(manager)
        assert event["actor"] == "mcp-stdio:7-7"
        assert event["details"]["attempted"] == "power_cycle"

    def test_bad_token_records_nothing(self, mcp, manager):
        mcp_server, _config = mcp
        _hold(manager, "a")
        with patch("labctl.mcp_server._confirmation_required", return_value=True):
            result = json.loads(mcp_server.power_cycle("a", confirm_token="bogus"))
        assert result["status"] == "confirmation_failed"
        assert _refusals(manager) == []

    def test_claimed_between_plan_and_confirm_records_once(self, mcp, manager):
        """The confirming call answers confirmation_failed (no refusal
        recorded); the retry is refused and recorded."""
        mcp_server, _config = mcp
        with patch("labctl.mcp_server._confirmation_required", return_value=True):
            token = json.loads(mcp_server.power_cycle("a"))["confirm_token"]
            _hold(manager, "a")
            failed = json.loads(mcp_server.power_cycle("a", confirm_token=token))
            assert failed["status"] == "confirmation_failed"
            assert _refusals(manager) == []
            mcp_server.power_cycle("a")
        assert len(_refusals(manager)) == 1

    def test_claim_required_and_not_operator(self, mcp, manager):
        mcp_server, config = mcp
        config.claims.require_claim = True
        _hold(manager, "b")
        with (
            patch("labctl.mcp_server._confirmation_required", return_value=False),
            patch("labctl.mcp_server._caller_is_operator", return_value=False),
        ):
            mcp_server.power_on("a")
            mcp_server.force_release_sbc("b", reason="x")
        reasons = [e["details"]["reason"] for e in _refusals(manager)]
        assert reasons == ["claim_required", "not_operator"]

    def test_refused_claim_attempt_recorded(self, mcp, manager):
        mcp_server, _config = mcp
        _hold(manager, "a")
        result = json.loads(mcp_server.claim_sbc("a", reason="want it"))
        assert result["error"] == "sbc_claimed"
        (event,) = _refusals(manager)
        assert event["details"]["attempted"] == "claim_sbc"

    def test_release_and_renew_of_someone_elses_claim(self, mcp, manager):
        mcp_server, _config = mcp
        _hold(manager, "a")
        assert json.loads(mcp_server.release_sbc("a"))["error"] == "not_claimant"
        assert json.loads(mcp_server.renew_sbc_claim("a"))["error"] == "not_claimant"
        events = _refusals(manager)
        assert [
            (e["details"]["reason"], e["details"]["attempted"]) for e in events
        ] == [
            ("not_claimant", "release_sbc"),
            ("not_claimant", "renew_sbc_claim"),
        ]
        assert {e["details"]["holder"] for e in events} == {"holder"}

    def test_allowed_call_records_no_refusal(self, mcp, manager):
        mcp_server, _config = mcp
        plug = MagicMock()
        plug.power_on.return_value = True
        with (
            patch("labctl.mcp_server._confirmation_required", return_value=False),
            patch("labctl.power.base.PowerController.from_plug", return_value=plug),
        ):
            mcp_server.power_on("a")
        assert _refusals(manager) == []


class TestCli:
    @pytest.fixture
    def cli(self, manager, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        return config

    def test_refused_command_recorded_once(self, cli, manager):
        """power off asks first: the pre-prompt check refuses it, and the
        refusal is recorded once."""
        from labctl.cli import main

        _hold(manager, "a")
        result = CliRunner().invoke(
            main, ["-c", str(cli), "power", "off", "a", "--yes"]
        )
        assert result.exit_code == 1
        (event,) = _refusals(manager)
        assert event["details"]["attempted"] == "labctl power off"
        assert event["source"] == "cli"

    def test_power_all_records_each_skipped_board(self, cli, manager):
        from labctl.cli import main

        _hold(manager, "a")
        plug = MagicMock()
        plug.power_on.return_value = True
        with patch("labctl.power.base.PowerController.from_plug", return_value=plug):
            result = CliRunner().invoke(
                main, ["-c", str(cli), "power-all", "on", "--yes"]
            )
        assert result.exit_code == 0, result.output
        events = _refusals(manager)
        assert [(e["entity_name"], e["details"]["attempted"]) for e in events] == [
            ("a", "labctl power-all")
        ]

    def test_release_of_someone_elses_claim(self, cli, manager):
        from labctl.cli import main

        _hold(manager, "a")
        result = CliRunner().invoke(main, ["-c", str(cli), "release", "a"])
        assert result.exit_code == 1
        (event,) = _refusals(manager)
        assert event["details"]["reason"] == "not_claimant"
        assert event["details"]["attempted"] == "labctl release"

    def test_dry_run_records_nothing(self, cli, manager):
        from labctl.cli import main

        _hold(manager, "a")
        result = CliRunner().invoke(
            main, ["-c", str(cli), "power", "cycle", "a", "--dry-run"]
        )
        assert result.exit_code == 1
        assert _refusals(manager) == []

    def test_allowed_command_records_no_refusal(self, cli, manager):
        from labctl.cli import main

        plug = MagicMock()
        plug.power_off.return_value = True
        with patch("labctl.power.base.PowerController.from_plug", return_value=plug):
            CliRunner().invoke(main, ["-c", str(cli), "power", "off", "a", "--yes"])
        assert _refusals(manager) == []
