"""Claim enforcement core (D013): roles, require_claim, claim owners and
ResourceManager.check_claim."""

import logging
import sqlite3

import pytest

from labctl.core import audit
from labctl.core.config import ROLE_AGENT, ROLE_OPERATOR, Config
from labctl.core.database import SCHEMA_VERSION, get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import (
    ClaimActor,
    ClaimConflict,
    ClaimRequiredError,
    NotClaimantError,
    NotOperatorError,
)


@pytest.fixture
def manager(tmp_path):
    m = ResourceManager(get_database(tmp_path / "lab.db"))
    m.create_sbc(name="pi-1")
    return m


def _claim(manager, session_id="s-alice", owner="alice", kind="mcp-http"):
    return manager.claim_sbc(
        sbc_name="pi-1",
        agent_name=owner or "anon",
        session_id=session_id,
        session_kind=kind,
        duration_seconds=600,
        reason="testing",
        owner=owner,
    )


class TestRoleParsing:
    def _role(self, entry, caplog=None):
        config = Config.from_dict(
            {"auth": {"users": [{"username": "u", "api_key": "k", **entry}]}}
        )
        return config.auth.users[0]

    def test_missing_role_is_operator(self):
        user = self._role({})
        assert user.role == ROLE_OPERATOR and user.is_operator

    def test_blank_role_is_operator(self):
        assert self._role({"role": None}).role == ROLE_OPERATOR
        assert self._role({"role": ""}).role == ROLE_OPERATOR

    def test_agent(self):
        user = self._role({"role": "agent"})
        assert user.role == ROLE_AGENT and not user.is_operator

    def test_case_and_whitespace_normalized(self):
        assert self._role({"role": " Operator "}).role == ROLE_OPERATOR

    @pytest.mark.parametrize("bad", ["operater", "admin", 1, True, ["operator"]])
    def test_unknown_role_is_agent_with_warning(self, bad, caplog):
        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            assert self._role({"role": bad}).role == ROLE_AGENT
        assert "unknown role" in caplog.text


class TestRequireClaimParsing:
    def test_default_off(self):
        assert Config.from_dict({}).claims.require_claim is False

    def test_true(self):
        config = Config.from_dict({"claims": {"require_claim": True}})
        assert config.claims.require_claim is True

    def test_blank_keeps_default(self):
        config = Config.from_dict({"claims": {"require_claim": None}})
        assert config.claims.require_claim is False

    def test_non_bool_keeps_default_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            config = Config.from_dict({"claims": {"require_claim": "always"}})
        assert config.claims.require_claim is False
        assert "require_claim" in caplog.text


class TestOwnerColumn:
    def test_schema_version(self):
        assert SCHEMA_VERSION == 9

    def test_claim_records_owner(self, manager):
        claim = _claim(manager)
        assert claim.owner == "alice"
        assert manager.get_active_claim("pi-1").owner == "alice"
        assert claim.to_dict()["owner"] == "alice"

    def test_unauthenticated_claim_has_no_owner(self, manager):
        claim = _claim(manager, owner=None, kind="cli")
        assert claim.owner is None
        assert "owner" not in claim.to_dict()

    def test_migration_v8_adds_owner_and_keeps_claims(self, tmp_path):
        path = tmp_path / "old.db"
        m = ResourceManager(get_database(path))
        m.create_sbc(name="pi-1")
        _claim(m, owner=None, kind="cli")
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE claims DROP COLUMN owner")
        conn.execute("DELETE FROM schema_version WHERE version > 8")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (8)")
        conn.commit()
        conn.close()

        db = get_database(path)
        db._reset_initialized()
        db.initialize()
        cols = {r["name"] for r in db.execute("PRAGMA table_info(claims)")}
        assert "owner" in cols
        assert ResourceManager(db).get_active_claim("pi-1").session_id == "s-alice"


class TestClaimActorHolds:
    def test_same_session(self, manager):
        claim = _claim(manager, owner=None)
        assert ClaimActor("s-alice").holds(claim)

    def test_same_owner_other_session(self, manager):
        claim = _claim(manager)
        assert ClaimActor("other-session", owner="alice").holds(claim)

    def test_other_owner(self, manager):
        claim = _claim(manager)
        assert not ClaimActor("s-bob", owner="bob").holds(claim)

    def test_no_owner_never_matches_ownerless_claim(self, manager):
        """Two unauthenticated callers don't share a claim through a
        missing owner."""
        claim = _claim(manager, owner=None)
        assert not ClaimActor("other-session", owner=None).holds(claim)
        assert not ClaimActor("other-session", owner="").holds(claim)


class TestCheckClaim:
    def test_unclaimed_proceeds(self, manager):
        assert manager.check_claim("pi-1", ClaimActor("s")) is None

    def test_require_claim_refuses_unclaimed(self, manager):
        with pytest.raises(ClaimRequiredError):
            manager.check_claim("pi-1", ClaimActor("s"), require_claim=True)

    def test_own_claim_proceeds_even_with_require_claim(self, manager):
        _claim(manager)
        actor = ClaimActor("s-alice", owner="alice")
        assert manager.check_claim("pi-1", actor, require_claim=True) is not None

    def test_own_claim_from_other_session_proceeds_and_heartbeats(self, manager):
        before = _claim(manager)
        claim = manager.check_claim("pi-1", ClaimActor("new-session", owner="alice"))
        assert claim.id == before.id
        assert manager.get_active_claim("pi-1").last_activity >= before.last_activity

    def test_other_claim_refused(self, manager):
        _claim(manager)
        with pytest.raises(ClaimConflict) as exc:
            manager.check_claim("pi-1", ClaimActor("s-bob", owner="bob"))
        assert exc.value.claim.owner == "alice"

    def test_agent_cannot_override(self, manager):
        _claim(manager)
        actor = ClaimActor("s-bob", owner="bob", operator=False)
        with pytest.raises(NotOperatorError):
            manager.check_claim("pi-1", actor, override=True)

    def test_operator_override_is_logged(self, manager):
        _claim(manager)
        actor = ClaimActor("s-op", owner="op", operator=True)
        with audit.activity_context("cli:op", "cli"):
            claim = manager.check_claim("pi-1", actor, override=True)
        assert claim.owner == "alice"
        events = audit.query_events(manager.db, limit=5, sbc="pi-1")
        assert any(e["action"] == "claim_override" for e in events)
        # The claim itself stays with its holder.
        assert manager.get_active_claim("pi-1").owner == "alice"

    def test_override_without_claim_is_a_no_op(self, manager):
        assert manager.check_claim("pi-1", ClaimActor("s"), override=True) is None


class TestOwnerReleaseAndRenew:
    def test_owner_releases_from_another_session(self, manager):
        _claim(manager)
        manager.release_claim("pi-1", "restarted-server:alice", owner="alice")
        assert manager.get_active_claim("pi-1") is None

    def test_other_owner_cannot_release(self, manager):
        _claim(manager)
        with pytest.raises(NotClaimantError):
            manager.release_claim("pi-1", "s-bob", owner="bob")

    def test_owner_renews_from_another_session(self, manager):
        _claim(manager)
        renewed = manager.renew_claim("pi-1", "s2", duration_seconds=900, owner="alice")
        assert renewed.duration_seconds == 900

    def test_session_match_still_works_without_owner(self, manager):
        _claim(manager, owner=None, kind="cli")
        manager.release_claim("pi-1", "s-alice")
        assert manager.get_active_claim("pi-1") is None


class TestDeadSessionSweep:
    def test_mcp_http_claims_are_swept(self, manager, monkeypatch):
        manager.claim_sbc(
            sbc_name="pi-1",
            agent_name="alice",
            session_id="mcp-http:999999-1:alice",
            session_kind="mcp-http",
            duration_seconds=60,
            reason="x",
            owner="alice",
        )
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = '2000-01-01 00:00:00'"
        )
        monkeypatch.setattr(manager, "_is_session_alive", lambda pid, epoch: False)
        assert manager.release_dead_sessions(grace_seconds=0) == 1
