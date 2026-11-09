"""Claims kept for an MCP session, renew on use, and warnings before and
after a claim lapses (#73)."""

import json
import logging
import sqlite3
from unittest.mock import patch

import pytest

from labctl.core.config import ClaimsConfig, Config
from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import (
    ClaimActor,
    ClaimHold,
    ClaimRequiredError,
    PlugType,
    ReleaseReason,
)

ALICE = ClaimActor(session_id="s-alice")
BOB = ClaimActor(session_id="s-bob")


@pytest.fixture
def manager(tmp_path):
    m = ResourceManager(get_database(tmp_path / "lab.db"))
    m.create_sbc(name="pi-1")
    m.create_sbc(name="pi-2")
    return m


def _claim(manager, sbc="pi-1", actor=ALICE, hold="fixed", seconds=600, **kw):
    return manager.claim_sbc(
        sbc_name=sbc,
        agent_name=actor.session_id,
        session_id=actor.session_id,
        session_kind=kw.pop("kind", "mcp-stdio"),
        duration_seconds=seconds,
        reason="testing",
        owner=actor.owner,
        hold=hold,
        **kw,
    )


# ---------------------------------------------------------------------------
# Core: hold, heartbeat, renew, lapse
# ---------------------------------------------------------------------------


class TestHoldColumn:
    def test_default_is_fixed(self, manager):
        claim = _claim(manager)
        assert claim.hold == ClaimHold.FIXED.value
        assert not claim.session_held
        assert claim.to_dict()["hold"] == "fixed"

    def test_session_hold_stored(self, manager):
        claim = _claim(manager, hold="session", seconds=1800)
        assert manager.get_active_claim("pi-1").session_held
        assert claim.to_dict()["hold"] == "session"
        assert claim.duration_seconds == 1800

    def test_unknown_hold_refused(self, manager):
        with pytest.raises(ValueError):
            _claim(manager, hold="forever")

    def test_migration_v14_marks_existing_claims_fixed(self, tmp_path):
        path = tmp_path / "old.db"
        get_database(path)  # current schema
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE claims DROP COLUMN hold")
        conn.execute("INSERT INTO sbcs (name) VALUES ('pi-1')")
        conn.execute(
            "INSERT INTO claims (sbc_id, agent_name, session_id, session_kind,"
            " reason, duration_seconds, expires_at)"
            " VALUES (1, 'a', 's', 'cli', 'r', 60, '2999-01-01 00:00:00')"
        )
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (14)")
        conn.commit()
        conn.close()
        m = ResourceManager(get_database(path))
        assert m.get_active_claim("pi-1").hold == "fixed"
        row = m.db.execute_one("SELECT MAX(version) AS v FROM schema_version")
        assert row["v"] == 15


class TestSessionHeartbeat:
    def test_extends_session_claims_only(self, manager, clock):
        session = _claim(manager, hold="session", seconds=600)
        fixed = _claim(manager, sbc="pi-2", seconds=600)
        clock.advance(300)
        assert manager.heartbeat_session_claims(ALICE) == 1
        assert manager.get_active_claim("pi-1").expires_at > session.expires_at
        assert manager.get_active_claim("pi-2").expires_at == fixed.expires_at

    def test_only_the_holders(self, manager, clock):
        claim = _claim(manager, hold="session")
        clock.advance(60)
        assert manager.heartbeat_session_claims(BOB) == 0
        assert manager.get_active_claim("pi-1").expires_at == claim.expires_at

    def test_owner_from_another_session(self, manager, clock):
        alice = ClaimActor(session_id="mcp-http:1-1:alice", owner="alice")
        _claim(manager, actor=alice, hold="session", kind="mcp-http")
        clock.advance(60)
        later = ClaimActor(session_id="mcp-http:2-2:alice", owner="alice")
        assert manager.heartbeat_session_claims(later) == 1

    def test_never_revives_an_expired_claim(self, manager, clock):
        _claim(manager, hold="session", seconds=600)
        clock.advance(601)
        assert manager.heartbeat_session_claims(ALICE) == 0
        assert manager.get_active_claim("pi-1") is None

    def test_six_hours_of_heartbeats_keep_it(self, manager, clock):
        _claim(manager, hold="session", seconds=1800)
        for _ in range(18):  # 6 h in 20-minute steps
            clock.advance(20 * 60)
            assert manager.heartbeat_session_claims(ALICE) == 1
        assert manager.get_active_claim("pi-1") is not None


class TestRenew:
    def test_session_claim_keeps_its_window(self, manager, clock):
        _claim(manager, hold="session", seconds=1800)
        clock.advance(60)
        renewed = manager.renew_claim("pi-1", "s-alice", duration_seconds=7200)
        assert renewed.session_held
        assert renewed.duration_seconds == 1800

    def test_switch_session_to_fixed(self, manager):
        _claim(manager, hold="session", seconds=1800)
        renewed = manager.renew_claim(
            "pi-1", "s-alice", duration_seconds=7200, hold="fixed"
        )
        assert not renewed.session_held
        assert renewed.duration_seconds == 7200

    def test_switch_fixed_to_session(self, manager):
        _claim(manager, seconds=600)
        renewed = manager.renew_claim(
            "pi-1", "s-alice", duration_seconds=1800, hold="session"
        )
        assert renewed.session_held
        assert renewed.duration_seconds == 1800

    def test_fixed_renew_unchanged(self, manager):
        _claim(manager, seconds=600)
        assert manager.renew_claim("pi-1", "s-alice", 900).duration_seconds == 900


class TestRenewOnUse:
    """Proposal 2 was already in place: a call on the board by its holder
    extends a fixed claim by its duration."""

    def test_check_claim_extends_a_fixed_claim(self, manager, clock):
        claim = _claim(manager, seconds=600)
        clock.advance(500)
        manager.check_claim("pi-1", ALICE)
        renewed = manager.get_active_claim("pi-1")
        assert (renewed.expires_at - claim.expires_at).total_seconds() == 500
        clock.advance(500)  # past the original deadline
        assert manager.get_active_claim("pi-1") is not None

    def test_refused_call_does_not_extend(self, manager, clock):
        claim = _claim(manager, actor=BOB, seconds=600)
        clock.advance(100)
        with pytest.raises(Exception):
            manager.check_claim("pi-1", ALICE)
        assert manager.get_active_claim("pi-1").expires_at == claim.expires_at


class TestLapsedClaim:
    def test_expired(self, manager, clock):
        _claim(manager, seconds=60)
        clock.advance(61)
        lapsed = manager.lapsed_claim("pi-1", ALICE)
        assert lapsed is not None and lapsed.lapsed
        assert "expired at" in lapsed.lapse_text()

    def test_expired_and_swept(self, manager, clock):
        _claim(manager, seconds=60)
        clock.advance(200)
        assert manager.expire_stale_claims(grace_seconds=60) == 1
        assert "expired at" in manager.lapsed_claim("pi-1", ALICE).lapse_text()

    def test_force_released(self, manager):
        _claim(manager)
        manager.force_release_claim("pi-1", reason="need it", released_by="op")
        text = manager.lapsed_claim("pi-1", ALICE).lapse_text()
        assert "force-released by 'op'" in text

    def test_session_lost(self, manager):
        claim = _claim(manager)
        manager.db.execute_modify(
            "UPDATE claims SET released_at = CURRENT_TIMESTAMP,"
            " release_reason = ? WHERE id = ?",
            (ReleaseReason.SESSION_LOST.value, claim.id),
        )
        assert "session ended" in manager.lapsed_claim("pi-1", ALICE).lapse_text()

    def test_released_by_holder_is_not_a_lapse(self, manager):
        _claim(manager)
        manager.release_claim("pi-1", "s-alice")
        assert manager.lapsed_claim("pi-1", ALICE) is None

    def test_not_after_someone_else_claimed_the_board(self, manager, clock):
        _claim(manager, seconds=60)
        clock.advance(200)
        _claim(manager, actor=BOB)
        manager.release_claim("pi-1", "s-bob")
        assert manager.lapsed_claim("pi-1", ALICE) is None

    def test_other_callers_lapse_is_not_yours(self, manager, clock):
        _claim(manager, actor=BOB, seconds=60)
        clock.advance(61)
        assert manager.lapsed_claim("pi-1", ALICE) is None

    def test_active_claim_is_not_lapsed(self, manager):
        _claim(manager)
        assert manager.lapsed_claim("pi-1", ALICE) is None

    def test_latest_claims_of(self, manager, clock):
        _claim(manager, seconds=60)
        _claim(manager, sbc="pi-2", actor=BOB)
        clock.advance(61)
        claims = manager.latest_claims_of(ALICE)
        assert [c.sbc_name for c in claims] == ["pi-1"]
        assert claims[0].lapsed


class TestStrictModeNamesTheLapse:
    def test_refusal_names_the_expired_claim(self, manager, clock):
        _claim(manager, seconds=60)
        clock.advance(61)
        with pytest.raises(ClaimRequiredError) as exc:
            manager.check_claim("pi-1", ALICE, require_claim=True)
        assert exc.value.lapsed is not None
        assert "your claim on 'pi-1'" in str(exc.value)
        assert "expired at" in str(exc.value)

    def test_plain_refusal_without_a_lapse(self, manager):
        with pytest.raises(ClaimRequiredError) as exc:
            manager.check_claim("pi-1", ALICE, require_claim=True)
        assert exc.value.lapsed is None
        assert str(exc.value) == (
            "SBC 'pi-1' must be claimed first (claims.require_claim is on)"
        )


class TestDeadSessions:
    def _dead(self, manager, claim):
        manager.db.execute_modify(
            "UPDATE claims SET session_id = ? WHERE id = ?",
            (f"{claim.session_kind}:999999999-1", claim.id),
        )

    def test_stdio_session_hold_released_at_once(self, manager):
        claim = _claim(
            manager, actor=ClaimActor("mcp-stdio:1-1"), hold="session", seconds=1800
        )
        self._dead(manager, claim)
        with patch.object(manager, "_is_session_alive", return_value=False):
            assert manager.release_dead_sessions(grace_seconds=60) == 1
        assert manager.lapsed_claim("pi-1", ClaimActor("mcp-stdio:999999999-1"))

    def test_fixed_stdio_claim_waits_for_its_deadline(self, manager):
        claim = _claim(manager, actor=ClaimActor("mcp-stdio:1-1"), seconds=1800)
        self._dead(manager, claim)
        with patch.object(manager, "_is_session_alive", return_value=False):
            assert manager.release_dead_sessions(grace_seconds=60) == 0

    def test_http_session_hold_waits_for_its_deadline(self, manager):
        """Its holder's next session (a restarted server) still holds it."""
        claim = _claim(
            manager,
            actor=ClaimActor("mcp-http:1-1:alice", owner="alice"),
            hold="session",
            kind="mcp-http",
        )
        self._dead(manager, claim)
        with patch.object(manager, "_is_session_alive", return_value=False):
            assert manager.release_dead_sessions(grace_seconds=60) == 0


class TestConfig:
    def test_default(self):
        assert Config.from_dict({}).claims.session_timeout_minutes == 30

    def test_set(self):
        config = Config.from_dict({"claims": {"session_timeout_minutes": 5}})
        assert config.claims.session_timeout_minutes == 5
        assert config.to_dict()["claims"]["session_timeout_minutes"] == 5

    @pytest.mark.parametrize("bad", [0, -1, "x", True, 1.5])
    def test_bad_value_keeps_default(self, bad, caplog):
        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            config = Config.from_dict({"claims": {"session_timeout_minutes": bad}})
        assert config.claims.session_timeout_minutes == 30
        assert "session_timeout_minutes" in caplog.text

    def test_clamped_to_claim_bounds(self):
        claims = ClaimsConfig(max_duration_minutes=20, session_timeout_minutes=30)
        warnings = claims.validate()
        assert claims.session_timeout_minutes == 20
        assert any("session_timeout_minutes" in w for w in warnings)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    config = Config()
    config.claims = ClaimsConfig(
        enabled=True,
        default_duration_minutes=30,
        max_duration_minutes=600,
        session_timeout_minutes=30,
    )
    return config


@pytest.fixture
def mcp(manager, config):
    """The MCP server as one stdio session, on ``manager``'s database."""
    import labctl.mcp_server as srv

    manager.assign_power_plug(
        sbc_id=manager.get_sbc_by_name("pi-1").id,
        plug_type=PlugType.TASMOTA,
        address="192.168.1.200",
    )
    with (
        patch.object(srv, "_get_manager", return_value=manager),
        patch.object(srv, "_get_config", return_value=config),
        patch.object(srv, "_SESSION_ID", "mcp-stdio:100-1"),
        patch.object(srv, "_SESSION_KIND", "mcp-stdio"),
        patch.object(srv, "_AGENT_NAME", "agent-a"),
        patch.object(srv, "_PARENT_PID", None),
        patch.object(srv, "_REPORTED_LAPSES", set()),
        patch.object(srv, "_confirmation_required", return_value=False),
    ):
        yield srv


@pytest.fixture
def http(mcp):
    """The same server over HTTP, called by authenticated user alice."""
    with (
        patch.object(mcp, "_SESSION_ID", "mcp-http:100-1"),
        patch.object(mcp, "_SESSION_KIND", "mcp-http"),
        patch.object(mcp, "_authenticated_user", return_value="alice"),
    ):
        yield mcp


@pytest.fixture
def power():
    with patch("labctl.power.base.PowerController.from_plug") as from_plug:
        from_plug.return_value.power_on.return_value = True
        yield from_plug


def _me(srv):
    return srv._caller_actor()


class TestMcpClaimSbcHold:
    def test_session_hold(self, mcp, manager):
        result = json.loads(mcp.claim_sbc("pi-1", hold="session"))
        assert result["status"] == "claimed"
        assert result["claim"]["hold"] == "session"
        assert "ends" in result["session_hold"]
        claim = manager.get_active_claim("pi-1")
        assert claim.session_held and claim.duration_seconds == 30 * 60

    def test_duration_ignored_with_a_note(self, mcp, manager):
        result = json.loads(mcp.claim_sbc("pi-1", hold="session", duration_minutes=90))
        assert "not used" in result["note"]
        assert manager.get_active_claim("pi-1").duration_seconds == 30 * 60

    def test_case_and_spaces(self, mcp):
        result = json.loads(mcp.claim_sbc("pi-1", hold=" Session "))
        assert result["claim"]["hold"] == "session"

    def test_none_means_fixed(self, mcp):
        result = json.loads(mcp.claim_sbc("pi-1", hold=None))
        assert result["claim"]["hold"] == "fixed"

    def test_invalid_hold(self, mcp, manager):
        result = json.loads(mcp.claim_sbc("pi-1", hold="forever"))
        assert result["error"] == "invalid_hold"
        assert manager.get_active_claim("pi-1") is None

    def test_fixed_by_default(self, mcp):
        result = json.loads(mcp.claim_sbc("pi-1", duration_minutes=10))
        assert result["claim"]["hold"] == "fixed"
        assert "session_hold" not in result

    def test_reclaim_switches_hold_both_ways(self, mcp, manager):
        mcp.claim_sbc("pi-1", duration_minutes=10)
        result = json.loads(mcp.claim_sbc("pi-1", hold="session"))
        assert result["status"] == "already_claimed"
        assert "held for this session" in result["message"]
        assert manager.get_active_claim("pi-1").session_held
        result = json.loads(mcp.claim_sbc("pi-1", duration_minutes=45))
        claim = manager.get_active_claim("pi-1")
        assert not claim.session_held and claim.duration_seconds == 45 * 60

    def test_unauthenticated_http_refused(self, mcp, manager):
        with (
            patch.object(mcp, "_SESSION_KIND", "mcp-http"),
            patch.object(mcp, "_authenticated_user", return_value=None),
        ):
            result = json.loads(mcp.claim_sbc("pi-1", hold="session"))
        assert result["error"] == "session_hold_unavailable"
        assert manager.get_active_claim("pi-1") is None

    def test_authenticated_http(self, http, manager):
        result = json.loads(http.claim_sbc("pi-1", hold="session"))
        assert "each tool call you make" in result["session_hold"]
        assert manager.get_active_claim("pi-1").owner == "alice"

    def test_renew_session_claim_with_duration_notes_it(self, mcp, manager):
        mcp.claim_sbc("pi-1", hold="session")
        result = json.loads(mcp.renew_sbc_claim("pi-1", duration_minutes=120))
        assert result["status"] == "renewed"
        assert "not applied" in result["note"]
        assert manager.get_active_claim("pi-1").duration_seconds == 30 * 60

    def test_renew_fixed_claim_has_no_note(self, mcp):
        mcp.claim_sbc("pi-1", duration_minutes=10)
        assert "note" not in json.loads(mcp.renew_sbc_claim("pi-1", 20))


class TestMcpSessionLifetime:
    def test_stdio_six_hours_calls_twenty_minutes_apart(self, mcp, manager, clock):
        """The acceptance case: no explicit renewals, the server process
        heartbeats (sweep thread) and calls come 20 minutes apart."""
        mcp.claim_sbc("pi-1", hold="session")
        for _ in range(18):
            clock.advance(10 * 60)
            mcp._heartbeat_process_session()
            clock.advance(10 * 60)
            mcp._heartbeat_process_session()
            json.loads(mcp.get_claim("pi-1"))
        assert manager.get_active_claim("pi-1") is not None

    def test_stdio_process_alone_keeps_it(self, mcp, manager, clock):
        """Long host-side work: no calls at all for hours."""
        mcp.claim_sbc("pi-1", hold="session")
        for _ in range(6 * 120):  # 6 h of 30 s sweeps
            clock.advance(30)
            mcp._heartbeat_process_session()
        assert manager.get_active_claim("pi-1") is not None

    def test_http_six_hours_calls_twenty_minutes_apart(self, http, manager, clock):
        """Over HTTP the calls are the heartbeat: any tool call counts."""
        http.claim_sbc("pi-1", hold="session")
        for _ in range(18):
            clock.advance(20 * 60)
            http.list_claims()
        assert manager.get_active_claim("pi-1") is not None

    def test_http_expires_without_calls(self, http, manager, clock):
        http.claim_sbc("pi-1", hold="session")
        http._heartbeat_process_session()  # not over HTTP
        clock.advance(31 * 60)
        assert manager.get_active_claim("pi-1") is None

    def test_fixed_claim_still_lapses_between_calls(self, mcp, manager, clock):
        """What happened in #73: a fixed claim and two hours of host work."""
        mcp.claim_sbc("pi-1", duration_minutes=120)
        clock.advance(121 * 60)
        mcp._heartbeat_process_session()
        assert manager.get_active_claim("pi-1") is None

    def test_client_gone_stops_the_process_heartbeat(self, mcp, manager, clock, caplog):
        mcp.claim_sbc("pi-1", hold="session")
        with (
            patch.object(mcp, "_PARENT_PID", 4242),
            patch.object(mcp, "_PARENT_GONE_WARNED", False),
            patch("os.getppid", return_value=1),
            caplog.at_level(logging.WARNING, logger="labctl.mcp_server"),
        ):
            clock.advance(25 * 60)
            mcp._heartbeat_process_session()
            clock.advance(6 * 60)
            mcp._heartbeat_process_session()
        assert "is gone" in caplog.text
        assert manager.get_active_claim("pi-1") is None

    def test_other_sessions_claims_not_heartbeated(self, mcp, manager, clock):
        _claim(manager, actor=ClaimActor("mcp-stdio:200-2"), hold="session")
        clock.advance(25 * 60)
        mcp._heartbeat_process_session()
        mcp.list_claims()
        clock.advance(6 * 60)
        assert manager.get_active_claim("pi-1") is None


class TestMcpClaimStatus:
    def test_text_result_says_until_when(self, mcp, power):
        mcp.claim_sbc("pi-1", duration_minutes=60)
        result = mcp.power_on("pi-1")
        assert "Power ON" in result
        assert "[claim] pi-1: yours until " in result
        assert "each call on the board extends it" in result

    def test_session_hold_in_text(self, mcp, power):
        mcp.claim_sbc("pi-1", hold="session")
        assert "[claim] pi-1: yours, held for this MCP session" in mcp.power_on("pi-1")

    def test_json_result_gets_claim_status(self, mcp):
        mcp.claim_sbc("pi-1", duration_minutes=60)
        result = json.loads(mcp.get_claim("pi-1"))
        status = result["claim_status"]["pi-1"]
        assert status["held"] is True and status["hold"] == "fixed"
        assert 59 * 60 <= status["claim_expires_in"] <= 60 * 60
        assert "claim_warnings" not in result

    def test_nothing_added_without_claims(self, mcp, power):
        """Single-agent labs never claim: results are unchanged."""
        assert mcp.power_on("pi-1") == "Power ON: pi-1"
        assert "claim_status" not in json.loads(mcp.get_claim("pi-1"))

    def test_nothing_added_for_someone_elses_claim(self, mcp, manager):
        _claim(manager, actor=BOB)
        assert "claim_status" not in json.loads(mcp.get_claim("pi-1"))

    def test_nothing_after_you_released_it(self, mcp, power):
        mcp.claim_sbc("pi-1", duration_minutes=10)
        mcp.release_sbc("pi-1")
        assert "[claim" not in mcp.power_on("pi-1")

    def test_nothing_when_claims_disabled(self, mcp, config, power, clock):
        mcp.claim_sbc("pi-1", duration_minutes=10)
        clock.advance(11 * 60)
        config.claims.enabled = False
        assert "[claim" not in mcp.power_on("pi-1")

    def test_notices_never_fail_the_call(self, mcp, manager, power):
        mcp.claim_sbc("pi-1", duration_minutes=10)
        with patch.object(manager, "latest_claims_of", side_effect=RuntimeError):
            assert "Power ON" in mcp.power_on("pi-1")

    def test_refused_call_has_no_status_for_its_board(self, mcp, manager, power):
        _claim(manager, actor=BOB)
        result = json.loads(mcp.power_on("pi-1"))
        assert result["error"] == "sbc_claimed"
        assert "claim_status" not in result


class TestMcpExpiryWarning:
    def test_warns_on_any_call_when_close_to_expiry(self, mcp, clock):
        mcp.claim_sbc("pi-1", duration_minutes=30)
        clock.advance(22 * 60)
        result = json.loads(mcp.list_claims())
        [warning] = result["claim_warnings"]
        assert "Your claim on 'pi-1' expires in 8 min" in warning
        assert 'hold="session"' in warning

    def test_no_warning_with_time_left(self, mcp, clock):
        mcp.claim_sbc("pi-1", duration_minutes=30)
        clock.advance(15 * 60)
        assert "claim_warnings" not in json.loads(mcp.list_claims())

    def test_short_claim_warns_only_in_its_second_half(self, mcp, clock):
        mcp.claim_sbc("pi-1", duration_minutes=4)
        clock.advance(60)
        assert "claim_warnings" not in json.loads(mcp.list_claims())
        clock.advance(70)
        assert "claim_warnings" in json.loads(mcp.list_claims())

    def test_call_on_the_board_renews_so_no_warning(self, mcp, clock, power):
        mcp.claim_sbc("pi-1", duration_minutes=30)
        clock.advance(25 * 60)
        assert "[claim warning]" not in mcp.power_on("pi-1")

    def test_session_claim_never_warns(self, mcp, clock):
        mcp.claim_sbc("pi-1", hold="session")
        clock.advance(25 * 60)
        assert "claim_warnings" not in json.loads(mcp.list_claims())


class TestMcpLapseWarning:
    def test_mutation_after_lapse_says_so(self, mcp, clock, power):
        """Not strict: the call goes ahead, but no longer silently."""
        mcp.claim_sbc("pi-1", duration_minutes=120)
        clock.advance(121 * 60)
        result = mcp.power_on("pi-1")
        assert "Power ON" in result
        assert "[claim warning] Your claim on 'pi-1'" in result
        assert "expired at" in result
        assert "no longer protected" in result
        # ...on every call on that board, until it is claimed again.
        assert "[claim warning]" in mcp.power_on("pi-1")
        mcp.claim_sbc("pi-1", duration_minutes=10)
        assert "[claim warning]" not in mcp.power_on("pi-1")

    def test_get_claim_shows_the_lapse(self, mcp, clock):
        """How the #73 lapse was found: get_claim said claimed=false."""
        mcp.claim_sbc("pi-1", duration_minutes=120)
        clock.advance(121 * 60)
        result = json.loads(mcp.get_claim("pi-1"))
        assert result["claimed"] is False
        status = result["claim_status"]["pi-1"]
        assert status["held"] is False and status["lapsed"] is True
        assert status["claim"]["agent_name"] == "agent-a"
        assert result["claim_warnings"] == [status["warning"]]

    def test_force_release_is_reported(self, mcp, manager):
        mcp.claim_sbc("pi-1", duration_minutes=60)
        manager.force_release_claim("pi-1", reason="x", released_by="operator-bob")
        [warning] = json.loads(mcp.list_claims())["claim_warnings"]
        assert "force-released by 'operator-bob'" in warning

    def test_lapse_elsewhere_reported_once(self, mcp, clock):
        mcp.claim_sbc("pi-2", duration_minutes=10)
        clock.advance(11 * 60)
        assert "claim_warnings" in json.loads(mcp.list_claims())
        assert "claim_warnings" not in json.loads(mcp.list_claims())
        # ...but a call on that board still says so.
        assert "claim_status" in json.loads(mcp.get_claim("pi-2"))

    def test_strict_mode_refusal_names_the_expired_claim(
        self, mcp, config, clock, power
    ):
        config.claims.require_claim = True
        mcp.claim_sbc("pi-1", duration_minutes=120)
        clock.advance(121 * 60)
        result = json.loads(mcp.power_on("pi-1"))
        assert result["error"] == "claim_required"
        assert "your claim on 'pi-1'" in result["message"]
        assert "expired at" in result["message"]
        assert result["lapsed_claim"]["agent_name"] == "agent-a"
        assert any('hold="session"' in h for h in result["hints"])
        power.assert_not_called()

    def test_strict_mode_plain_refusal_without_lapse(self, mcp, config, power):
        config.claims.require_claim = True
        result = json.loads(mcp.power_on("pi-1"))
        assert result["error"] == "claim_required"
        assert "lapsed_claim" not in result
