"""Claim enforcement core (D013): roles, require_claim, claim owners and
ResourceManager.check_claim."""

import logging
import sqlite3
from datetime import datetime, timedelta

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


@pytest.fixture(autouse=True)
def _fresh_config_warnings():
    from labctl.core import config

    config._warned.clear()
    yield
    config._warned.clear()


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
        assert self._role({"role": "  "}).role == ROLE_OPERATOR

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


class TestConfigWarningsOnce:
    def test_same_bad_value_warns_once(self, caplog):
        entry = {"auth": {"users": [{"username": "u", "api_key": "k", "role": "x"}]}}
        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            Config.from_dict(entry)
            Config.from_dict(entry)
        assert caplog.text.count("unknown role") == 1


class TestRequireClaimParsing:
    def test_default_off(self):
        assert Config.from_dict({}).claims.require_claim is False

    def test_true(self):
        config = Config.from_dict({"claims": {"require_claim": True}})
        assert config.claims.require_claim is True

    @pytest.mark.parametrize("blank", [None, "", "  "])
    def test_blank_keeps_default(self, blank):
        config = Config.from_dict({"claims": {"require_claim": blank}})
        assert config.claims.require_claim is False

    @pytest.mark.parametrize(
        "value, expected",
        [("true", True), ("yes", True), ("always", True), (1, True)]
        + [("false", False), (" Off ", False), ("no", False), (0, False)],
    )
    def test_non_bool_warns_and_fails_safe_unless_plainly_off(
        self, value, expected, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            config = Config.from_dict({"claims": {"require_claim": value}})
        assert config.claims.require_claim is expected
        assert "require_claim" in caplog.text


class TestConfigRoundTrip:
    def test_role_and_require_claim_survive_to_dict(self):
        config = Config.from_dict(
            {
                "auth": {"users": [{"username": "u", "api_key": "k", "role": "agent"}]},
                "claims": {"require_claim": True},
            }
        )
        again = Config.from_dict(config.to_dict())
        assert again.auth.users[0].role == ROLE_AGENT
        assert again.claims.require_claim is True

    def test_require_claim_without_claims_warns(self):
        config = Config.from_dict({"claims": {"enabled": False, "require_claim": True}})
        assert any("require_claim" in w for w in config.claims.validate())


class TestRequireAgentNameParsing:
    @pytest.mark.parametrize("value, expected", [(True, True), ("false", False)])
    def test_parsed_like_require_claim(self, value, expected):
        config = Config.from_dict({"claims": {"require_agent_name": value}})
        assert config.claims.require_agent_name is expected


class TestRenewAfterExpiry:
    def test_renew_does_not_revive_an_expired_claim(self, manager, monkeypatch):
        from labctl.core.models import ClaimNotFoundError

        claim = _claim(manager)
        monkeypatch.setattr(manager, "get_active_claim", lambda name: claim)
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = '2000-01-01 00:00:00' WHERE id = ?",
            (claim.id,),
        )
        with pytest.raises(ClaimNotFoundError):
            manager.renew_claim("pi-1", "s-alice", owner="alice")


class TestRenewAfterRelease:
    def test_renew_does_not_revive_a_released_claim(self, manager, monkeypatch):
        from labctl.core.models import ClaimNotFoundError

        claim = _claim(manager)
        real = manager.get_active_claim
        # renew_claim loads the claim, then it is released elsewhere.
        monkeypatch.setattr(manager, "get_active_claim", lambda name: claim)
        manager.db.execute_modify(
            "UPDATE claims SET released_at = CURRENT_TIMESTAMP WHERE id = ?",
            (claim.id,),
        )
        with pytest.raises(ClaimNotFoundError):
            manager.renew_claim("pi-1", "s-alice", owner="alice")
        monkeypatch.setattr(manager, "get_active_claim", real)
        assert manager.get_active_claim("pi-1") is None


class TestOwnerColumn:
    def test_schema_version(self):
        assert SCHEMA_VERSION >= 9

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


class TestMigrationBackfillsOwner:
    def test_open_http_claims_get_their_owner(self, tmp_path):
        path = tmp_path / "old.db"
        m = ResourceManager(get_database(path))
        m.create_sbc(name="pi-1")
        m.create_sbc(name="pi-2")
        for name, sid in (
            ("pi-1", "mcp-stdio:123-456:fw-dev"),
            ("pi-2", "mcp-stdio:123-456"),
        ):
            m.claim_sbc(
                sbc_name=name,
                agent_name="a",
                session_id=sid,
                session_kind="mcp-stdio",
                duration_seconds=600,
                reason="x",
            )
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE claims DROP COLUMN owner")
        conn.execute("DELETE FROM schema_version WHERE version > 8")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (8)")
        conn.commit()
        conn.close()

        db = get_database(path)
        db._reset_initialized()
        db.initialize()
        m = ResourceManager(db)
        assert m.get_active_claim("pi-1").owner == "fw-dev"
        assert m.get_active_claim("pi-2").owner is None
        assert ClaimActor("mcp-http:999-1:fw-dev", "fw-dev").holds(
            m.get_active_claim("pi-1")
        )


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
        actor = ClaimActor("s", operator=True)
        assert manager.check_claim("pi-1", actor, override=True) is None

    def test_actor_defaults_to_not_operator(self, manager):
        _claim(manager)
        with pytest.raises(NotOperatorError):
            manager.check_claim("pi-1", ClaimActor("s-bob", "bob"), override=True)

    def test_operator_override_satisfies_require_claim(self, manager):
        actor = ClaimActor("s-op", owner="op", operator=True)
        assert (
            manager.check_claim("pi-1", actor, override=True, require_claim=True)
            is None
        )
        events = audit.query_events(manager.db, limit=5, sbc="pi-1")
        assert any(e["action"] == "claim_override" for e in events)

    def test_agent_override_with_require_claim_refused(self, manager):
        with pytest.raises(NotOperatorError):
            manager.check_claim(
                "pi-1", ClaimActor("s-bob", "bob"), override=True, require_claim=True
            )


class TestSharedHardware:
    @pytest.fixture
    def lab(self, manager):
        from labctl.core.models import PlugType

        manager.create_sbc(name="pi-2")
        for name in ("pi-1", "pi-2"):
            manager.assign_power_plug(
                sbc_id=manager.get_sbc_by_name(name).id,
                plug_type=PlugType.KASA,
                address="10.0.0.5",
                plug_index=2,
            )
        return manager

    def test_sharing_found_both_ways(self, lab):
        assert lab.sbcs_sharing_hardware_with("pi-1") == ["pi-2"]
        assert lab.sbcs_sharing_hardware_with("pi-2") == ["pi-1"]

    def test_claim_on_sharer_refuses(self, lab):
        _claim(lab)  # alice claims pi-1
        with pytest.raises(ClaimConflict) as exc:
            lab.check_claim("pi-2", ClaimActor("s-bob", "bob"))
        assert exc.value.claim.sbc_name == "pi-1"

    def test_include_shared_false_skips_sharers(self, lab):
        _claim(lab)
        actor = ClaimActor("s-bob", "bob")
        assert lab.check_claim("pi-2", actor, include_shared=False) is None

    def test_require_claim_only_for_named_board(self, lab):
        lab.claim_sbc(
            sbc_name="pi-2",
            agent_name="bob",
            session_id="s-bob",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="x",
            owner="bob",
        )
        actor = ClaimActor("s-bob", "bob")
        assert lab.check_claim("pi-2", actor, require_claim=True) is not None

    def test_serial_device_matched_by_registered_name(self, manager):
        from labctl.core.models import PortType

        manager.create_serial_device(name="port-1", usb_path="1-1.1")
        manager.assign_serial_port(
            sbc_id=manager.get_sbc_by_name("pi-1").id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/port-1",
        )
        assert manager.sbcs_sharing_hardware(serial_device_path="port-1") == ["pi-1"]

    def test_legacy_index_zero_shares_every_outlet(self, manager):
        from labctl.core.models import PlugType

        manager.create_sbc(name="pi-2")
        manager.assign_power_plug(
            sbc_id=manager.get_sbc_by_name("pi-1").id,
            plug_type=PlugType.TASMOTA,
            address="10.0.0.6",
            plug_index=1,
        )
        manager.db.execute_modify(
            "INSERT INTO power_plugs (sbc_id, plug_type, address, plug_index)"
            " VALUES (?, 'tasmota', '10.0.0.6', 0)",
            (manager.get_sbc_by_name("pi-2").id,),
        )
        assert manager.sbcs_sharing_hardware_with("pi-1") == ["pi-2"]
        assert manager.sbcs_sharing_hardware_with("pi-2") == ["pi-1"]

    def test_stored_address_whitespace_ignored(self, manager):
        from labctl.core.models import PlugType

        manager.db.execute_modify(
            "INSERT INTO power_plugs (sbc_id, plug_type, address, plug_index)"
            " VALUES (?, 'kasa', ' 10.0.0.7 ', 1)",
            (manager.get_sbc_by_name("pi-1").id,),
        )
        assert manager.sbcs_sharing_hardware(
            plug=(PlugType.KASA.value, "10.0.0.7", 1)
        ) == ["pi-1"]

    def test_serial_ports_linked_to_one_adapter_share(self, manager):
        from labctl.core.models import PortType

        dev = manager.create_serial_device(name="ad3", usb_path="1-1.3")
        manager.create_sbc(name="pi-2")
        for name, path in (("pi-1", "/dev/lab/ad3"), ("pi-2", "/dev/ttyUSB0")):
            manager.assign_serial_port(
                sbc_id=manager.get_sbc_by_name(name).id,
                port_type=PortType.CONSOLE,
                device_path=path,
                serial_device_id=dev.id,
            )
        assert manager.sbcs_sharing_hardware_with("pi-2") == ["pi-1"]

    def test_serial_paths_match_through_symlink(self, manager, tmp_path):
        from labctl.core.models import PortType

        real = tmp_path / "ttyUSB0"
        real.touch()
        link = tmp_path / "port-1"
        link.symlink_to(real)
        manager.assign_serial_port(
            sbc_id=manager.get_sbc_by_name("pi-1").id,
            port_type=PortType.CONSOLE,
            device_path=str(link),
        )
        assert manager.sbcs_sharing_hardware(serial_device_path=str(real)) == ["pi-1"]

    def test_deleted_sharer_does_not_stop_the_check(self, lab, monkeypatch):
        from labctl.core.models import PlugType, UnknownSBCError

        lab.create_sbc(name="pi-3")
        lab.assign_power_plug(
            sbc_id=lab.get_sbc_by_name("pi-3").id,
            plug_type=PlugType.KASA,
            address="10.0.0.5",
            plug_index=2,
        )
        lab.claim_sbc(
            sbc_name="pi-3",
            agent_name="carol",
            session_id="s-carol",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="x",
            owner="carol",
        )
        real = lab.get_active_claim

        def flaky(name):
            if name == "pi-2":
                raise UnknownSBCError(name)
            return real(name)

        monkeypatch.setattr(lab, "get_active_claim", flaky)
        with pytest.raises(ClaimConflict) as exc:
            lab.check_claim("pi-1", ClaimActor("s-bob", "bob"))
        assert exc.value.claim.sbc_name == "pi-3"

    def test_actuator_records_match_through_symlink_and_serial(self, manager, tmp_path):
        from labctl.core.models import ChannelState, DriverName

        real = tmp_path / "ttyUSB0"
        real.touch()
        link = tmp_path / "relay-a"
        link.symlink_to(real)
        ids = []
        for name, kwargs in (
            ("r1", {"device_path": str(link)}),
            ("r1b", {"device_path": str(real)}),
            (
                "r1c",
                {
                    "serial_no": "SN9",
                    "vid": "1a86",
                    "pid": "7523",
                    "device_path": str(link),
                },
            ),
            ("r1d", {"serial_no": "SN9", "vid": "1a86", "pid": "7523"}),
        ):
            a = manager.create_actuator(name, DriverName.LCUS1_SERIAL, **kwargs)
            manager.add_actuator_channel(a.id, 1, default_state=ChannelState.OPEN)
            ids.append(manager.get_actuator_channel(a.id, 1).id)
        assert manager.channels_on_same_device({ids[0]}) == set(ids)

    def test_vid_pid_case_and_prefix_normalised(self, manager):
        from labctl.core.models import ChannelState, DriverName

        ids = []
        for name, vid in (("ra", "1A86"), ("rb", "0x01a86")):
            a = manager.create_actuator(
                name,
                DriverName.LCUS1_SERIAL,
                device_path=f"/dev/lab/{name}",
                vid=vid,
                pid="7523",
                serial_no="X",
            )
            manager.add_actuator_channel(a.id, 1, default_state=ChannelState.OPEN)
            ids.append(manager.get_actuator_channel(a.id, 1).id)
        assert manager.channels_on_same_device({ids[0]}) == set(ids)

    def test_refused_call_does_not_heartbeat(self, lab):
        _claim(lab)  # alice holds pi-1
        lab.claim_sbc(
            sbc_name="pi-2",
            agent_name="bob",
            session_id="s-bob",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="x",
            owner="bob",
        )
        lab.db.execute_modify(
            "UPDATE claims SET last_activity = '2000-01-01 00:00:00'"
            " WHERE session_id = 's-alice'"
        )
        before = lab.get_active_claim("pi-1").last_activity
        with pytest.raises(ClaimConflict):
            lab.check_claim("pi-1", ClaimActor("s-alice", "alice"))
        assert lab.get_active_claim("pi-1").last_activity == before

    def test_heartbeat_does_not_revive_expired_claim(self, manager):
        claim = _claim(manager)
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = '2000-01-01 00:00:00' WHERE id = ?",
            (claim.id,),
        )
        manager.touch_claim(claim)
        row = manager.db.execute_one(
            "SELECT expires_at FROM claims WHERE id = ?", (claim.id,)
        )
        assert str(row["expires_at"]).startswith("2000-01-01")

    def test_same_serial_different_model_and_path_not_merged(self, manager):
        from labctl.core.models import ChannelState, DriverName

        ids = []
        for name, path in (("ra", "/dev/lab/relay-a"), ("rb", "/dev/lab/relay-b")):
            a = manager.create_actuator(
                name, DriverName.LCUS1_SERIAL, device_path=path, serial_no="0001"
            )
            manager.add_actuator_channel(a.id, 1, default_state=ChannelState.OPEN)
            ids.append(manager.get_actuator_channel(a.id, 1).id)
        assert manager.channels_on_same_device({ids[0]}) == {ids[0]}

    def test_using_a_board_does_not_heartbeat_sharers(self, lab):
        lab.claim_sbc(
            sbc_name="pi-2",
            agent_name="alice",
            session_id="s-alice-2",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="x",
            owner="alice",
        )
        lab.db.execute_modify(
            "UPDATE claims SET last_activity = '2000-01-01 00:00:00'"
            " WHERE session_id = 's-alice-2'"
        )
        before = lab.get_active_claim("pi-2").last_activity
        lab.check_claim("pi-1", ClaimActor("s-alice", "alice"))
        assert lab.get_active_claim("pi-2").last_activity == before

    def test_actuator_records_on_one_device_share_channels(self, manager):
        from labctl.core.models import ChannelState, DriverName

        ids = []
        for name in ("r1", "r1b"):
            a = manager.create_actuator(
                name, DriverName.LCUS1_SERIAL, device_path="/dev/lab/relay-a"
            )
            manager.add_actuator_channel(a.id, 1, default_state=ChannelState.OPEN)
            ids.append(manager.get_actuator_channel(a.id, 1).id)
        assert manager.channels_on_same_device({ids[1]}) == set(ids)


class TestReclaimInGrace:
    def _expire(self, manager, sbc_name, seconds_ago=5):
        sbc_id = manager.get_sbc_by_name(sbc_name).id
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = ? WHERE sbc_id = ?",
            (
                manager._fmt_ts(datetime.now() - timedelta(seconds=seconds_ago)),
                sbc_id,
            ),
        )

    def test_own_expired_claim_replaced_others_keep_grace(self, manager):
        manager.create_sbc(name="pi-2")
        _claim(manager)  # alice, pi-1
        manager.claim_sbc(
            sbc_name="pi-2",
            agent_name="bob",
            session_id="s-bob",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="x",
            owner="bob",
        )
        self._expire(manager, "pi-1")
        self._expire(manager, "pi-2")
        manager.claim_sbc(
            sbc_name="pi-2",
            agent_name="bob",
            session_id="s-bob-2",
            session_kind="mcp-http",
            duration_seconds=600,
            reason="again",
            owner="bob",
            grace_seconds=60,
        )
        # alice's expired pi-1 claim is still inside its grace period.
        with pytest.raises(ClaimConflict):
            manager.claim_sbc(
                sbc_name="pi-1",
                agent_name="carol",
                session_id="s-carol",
                session_kind="mcp-http",
                duration_seconds=600,
                reason="x",
                owner="carol",
                grace_seconds=60,
            )


class TestReclaimLogsReplacedClaim:
    def test_replaced_expired_claim_is_logged(self, manager):
        _claim(manager)
        # Expired, but inside the default 60 s grace period.
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = ?",
            (manager._fmt_ts(datetime.now() - timedelta(seconds=5)),),
        )
        _claim(manager, session_id="s-alice-2")
        actions = [
            e["action"] for e in audit.query_events(manager.db, limit=10, sbc="pi-1")
        ]
        assert "claim_expired" in actions


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


class TestListBindingsByChannel:
    def test_filters_and_empty(self, manager):
        from labctl.core.models import ChannelState, DriverName, ShapeMode

        a = manager.create_actuator("r", DriverName.LCUS1_SERIAL, device_path="/x")
        manager.add_actuator_channel(a.id, 1, default_state=ChannelState.OPEN)
        manager.add_actuator_channel(a.id, 2, default_state=ChannelState.OPEN)
        ch1 = manager.get_actuator_channel(a.id, 1)
        sbc = manager.get_sbc_by_name("pi-1")
        manager.create_binding(
            sbc.id,
            "reset",
            ch1.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
        )
        ch2 = manager.get_actuator_channel(a.id, 2)
        assert [b.purpose for b in manager.list_bindings(channel_ids={ch1.id})] == [
            "reset"
        ]
        assert manager.list_bindings(channel_ids={ch2.id}) == []
        assert manager.list_bindings(channel_ids=set()) == []


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
