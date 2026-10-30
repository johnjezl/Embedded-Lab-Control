"""Claim enforcement in the web UI and REST API (D013, part 3)."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.security import generate_password_hash

from labctl.core.config import (
    ROLE_AGENT,
    AuthConfig,
    ClaimsConfig,
    Config,
    Ser2NetConfig,
    SerialConfig,
    UserConfig,
)
from labctl.core.manager import get_manager
from labctl.core.models import AddressType, PlugType, PortType
from labctl.power import PowerState
from labctl.web.app import create_app

PASSWORD = "testpass123"
OPERATOR_KEY = "operator-key-0123456789"
AGENT_KEY = "agent-key-0123456789"
AGENT2_KEY = "agent2-key-0123456789"
OP = {"X-API-Key": OPERATOR_KEY}
BOT = {"X-API-Key": AGENT_KEY}
BOT2 = {"X-API-Key": AGENT2_KEY}


def _config(tmp_path, *, auth=True, **claims):
    users = [
        UserConfig(
            username="alice",
            password_hash=generate_password_hash(PASSWORD),
            api_key=OPERATOR_KEY,
        ),
        UserConfig(
            username="bot",
            password_hash=generate_password_hash(PASSWORD),
            api_key=AGENT_KEY,
            role=ROLE_AGENT,
        ),
        UserConfig(username="bot2", api_key=AGENT2_KEY, role=ROLE_AGENT),
    ]
    return Config(
        database_path=tmp_path / "test.db",
        serial=SerialConfig(dev_dir=Path("/dev/lab"), base_tcp_port=4000),
        ser2net=Ser2NetConfig(config_file=tmp_path / "ser2net.yaml", enabled=True),
        auth=AuthConfig(enabled=auth, secret_key="test-secret", users=users),
        claims=ClaimsConfig(**claims),
    )


def _make(tmp_path, **kwargs):
    config = _config(tmp_path, **kwargs)
    app = create_app(config)
    app.config["TESTING"] = True
    with app.app_context():
        manager = get_manager(config.database_path)
    return app, app.test_client(), manager


@pytest.fixture
def lab(tmp_path):
    """Auth on, claims on; boards a and b share a plug outlet, c is apart."""
    app, client, manager = _make(tmp_path)
    for name in ("a", "b", "c"):
        manager.create_sbc(name=name)
    for name in ("a", "b"):
        sbc = manager.get_sbc_by_name(name)
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
    c = manager.get_sbc_by_name("c")
    manager.assign_power_plug(c.id, PlugType.TASMOTA, "10.0.0.5", 2)
    return client, manager


def _claim(manager, sbc, *, agent="holder", session="other-session", owner=None):
    return manager.claim_sbc(
        sbc_name=sbc,
        agent_name=agent,
        session_id=session,
        session_kind="mcp-http",
        duration_seconds=600,
        reason="holding",
        owner=owner,
    )


def _overrides(manager, sbc=None):
    rows = manager.db.execute(
        "SELECT actor, entity_name, details FROM audit_log "
        "WHERE action = 'claim_override' ORDER BY id"
    )
    return [dict(r) for r in rows if sbc is None or r["entity_name"] == sbc]


@pytest.fixture
def power():
    with patch("labctl.web.api.PowerController") as cls:
        controller = MagicMock()
        controller.power_on.return_value = True
        controller.power_off.return_value = True
        controller.get_state.return_value = PowerState.ON
        cls.from_plug.return_value = controller
        yield controller


@pytest.fixture
def view_power():
    with patch("labctl.web.views.PowerController") as cls:
        controller = MagicMock()
        controller.power_on.return_value = True
        controller.get_state.return_value = PowerState.ON
        cls.from_plug.return_value = controller
        yield controller


def _login(client, username):
    html = client.get("/login").data.decode()
    token = re.search(r'name="_csrf_token" value="([^"]+)"', html).group(1)
    resp = client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "_csrf_token": token},
    )
    assert resp.status_code == 302


def _csrf(client, page="/"):
    html = client.get(page).data.decode()
    return re.search(r'name="_csrf_token" value="([^"]+)"', html).group(1)


# --- REST: operating a board ---


class TestRestPower:
    def test_refused_on_another_users_claim(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "sbc_claimed"
        assert body["sbc_name"] == "a"
        assert "'holder'" in body["message"]
        power.power_on.assert_not_called()

    def test_owner_from_another_session_may_act(self, lab, power):
        """A claim made by the same user over MCP counts as theirs."""
        client, manager = lab
        claim = _claim(manager, "a", owner="bot", session="mcp-http:1-2:bot")
        manager.db.execute_modify(
            "UPDATE claims SET last_activity = '2000-01-01 00:00:00' WHERE id = ?",
            (claim.id,),
        )
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 200
        power.power_on.assert_called_once()
        assert manager.get_active_claim("a").last_activity.year > 2000

    def test_shared_outlet_refused(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/b/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["sbc_name"] == "a"
        assert "shares hardware with 'b'" in body["message"]
        power.power_on.assert_not_called()

    def test_other_outlet_not_affected(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/c/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 200

    def test_agent_may_not_override(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/a/power",
            json={"action": "on", "override_claim": True},
            headers=BOT,
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_operator"
        power.power_on.assert_not_called()
        assert _overrides(manager) == []

    def test_operator_override_is_logged(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/a/power",
            json={"action": "on", "override_claim": True},
            headers=OP,
        )
        assert resp.status_code == 200
        power.power_on.assert_called_once()
        logged = _overrides(manager, "a")
        assert len(logged) == 1
        assert logged[0]["actor"] == "api:alice"
        assert "holder" in logged[0]["details"]

    def test_override_on_shared_board_logs_holders_board(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/b/power",
            json={"action": "on", "override_claim": True},
            headers=OP,
        )
        assert resp.status_code == 200
        assert len(_overrides(manager, "a")) == 1

    @pytest.mark.parametrize("value", ["true", 1, "yes"])
    def test_json_override_must_be_true(self, lab, power, value):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/a/power",
            json={"action": "on", "override_claim": value},
            headers=OP,
        )
        assert resp.status_code == 409
        power.power_on.assert_not_called()

    def test_operator_without_override_refused(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=OP)
        assert resp.status_code == 409
        hints = " ".join(resp.get_json()["hints"])
        assert "override_claim" in hints

    def test_agent_hints_omit_override(self, lab, power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert "override_claim" not in " ".join(resp.get_json()["hints"])

    def test_claims_disabled_checks_nothing(self, tmp_path, power):
        _, client, manager = _make(tmp_path, enabled=False)
        sbc = manager.create_sbc(name="a")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 200


class TestRestRequireClaim:
    @pytest.fixture
    def strict(self, tmp_path):
        _, client, manager = _make(tmp_path, require_claim=True)
        sbc = manager.create_sbc(name="a")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
        return client, manager

    def test_unclaimed_board_refused(self, strict, power):
        client, _ = strict
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "claim_required"
        power.power_on.assert_not_called()

    def test_own_claim_allowed(self, strict, power):
        client, _ = strict
        resp = client.post("/api/claims/a", json={"reason": "work"}, headers=BOT)
        assert resp.status_code == 201
        resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 200

    def test_config_change_needs_claim_too(self, strict):
        client, _ = strict
        resp = client.put("/api/sbcs/a", json={"project": "x"}, headers=BOT)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "claim_required"

    def test_operator_override_logged(self, strict, power):
        client, manager = strict
        resp = client.post(
            "/api/sbcs/a/power",
            json={"action": "on", "override_claim": True},
            headers=OP,
        )
        assert resp.status_code == 200
        assert len(_overrides(manager, "a")) == 1

    def test_agent_override_refused(self, strict, power):
        client, _ = strict
        resp = client.post(
            "/api/sbcs/a/power",
            json={"action": "on", "override_claim": True},
            headers=BOT,
        )
        assert resp.status_code == 403

    def test_web_ui_exempt(self, strict, view_power):
        client, _ = strict
        _login(client, "alice")
        resp = client.post(
            "/sbc/a/power/on", data={"_csrf_token": _csrf(client, "/sbc/a")}
        )
        assert resp.status_code == 302
        view_power.power_on.assert_called_once()


# --- REST: configuration ---


class TestRestConfiguration:
    def test_update_refused(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.put("/api/sbcs/a", json={"project": "x"}, headers=BOT)
        assert resp.status_code == 409
        assert manager.get_sbc_by_name("a").project is None

    def test_update_ignores_sharers_claim(self, lab):
        """A board's own records don't touch hardware it shares."""
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.put("/api/sbcs/b", json={"project": "x"}, headers=BOT)
        assert resp.status_code == 200

    def test_port_on_claimed_boards_device_refused(self, lab):
        client, manager = lab
        a = manager.get_sbc_by_name("a")
        manager.assign_serial_port(a.id, PortType.CONSOLE, "/dev/lab/port-a")
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/c/ports",
            json={"type": "console", "device": "/dev/lab/port-a"},
            headers=BOT,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["sbc_name"] == "a"
        assert "shares hardware with 'c'" in body["message"]
        assert manager.get_sbc_by_name("c").console_port is None

    def test_port_on_claimed_boards_tcp_port_refused(self, lab):
        client, manager = lab
        a = manager.get_sbc_by_name("a")
        manager.assign_serial_port(
            a.id, PortType.CONSOLE, "/dev/lab/port-a", tcp_port=4100
        )
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/c/ports",
            json={"type": "console", "device": "/dev/lab/port-c", "tcp_port": "4100"},
            headers=BOT,
        )
        assert resp.status_code == 409
        assert resp.get_json()["sbc_name"] == "a"

    @pytest.mark.parametrize(
        "tcp_port", ["²", 10**20, str(10**20), "1" * 5000, 0, 70000, "70000"]
    )
    def test_bad_tcp_port_is_400(self, lab, tcp_port):
        client, _ = lab
        resp = client.post(
            "/api/sbcs/c/ports",
            json={"type": "console", "device": "/dev/x", "tcp_port": tcp_port},
            headers=BOT,
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("tcp_port", [str(10**20), "70000", "1" * 5000])
    def test_web_bad_tcp_port_flashed(self, lab, tcp_port):
        client, manager = lab
        _login(client, "alice")
        resp = client.post(
            "/sbc/c/port/assign",
            data={
                "_csrf_token": _csrf(client, "/sbc/c"),
                "port_type": "console",
                "device": "/dev/x",
                "tcp_port": tcp_port,
            },
        )
        assert resp.status_code == 302
        assert manager.get_sbc_by_name("c").console_port is None

    def test_port_device_must_be_a_path(self, lab):
        client, _ = lab
        resp = client.post(
            "/api/sbcs/c/ports", json={"type": "console", "device": 5}, headers=BOT
        )
        assert resp.status_code == 400

    def test_port_operator_override(self, lab):
        client, manager = lab
        a = manager.get_sbc_by_name("a")
        manager.assign_serial_port(a.id, PortType.CONSOLE, "/dev/lab/port-a")
        _claim(manager, "a", owner="bot2")
        resp = client.post(
            "/api/sbcs/c/ports",
            json={
                "type": "debug",
                "device": "/dev/lab/port-a",
                "override_claim": True,
            },
            headers=OP,
        )
        assert resp.status_code == 201
        assert len(_overrides(manager, "a")) == 1


class TestRestDelete:
    def test_claimed_board_is_409_not_500(self, lab):
        """#21: a ClaimConflict from delete_sbc was a 500."""
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.delete("/api/sbcs/a", headers=BOT)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "sbc_claimed"
        assert manager.get_sbc_by_name("a") is not None

    def test_operator_override_deletes(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.delete("/api/sbcs/a?override_claim=1", headers=OP)
        assert resp.status_code == 200
        assert manager.get_sbc_by_name("a") is None

    def test_agent_override_refused(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.delete("/api/sbcs/a?override_claim=1", headers=BOT)
        assert resp.status_code == 403
        assert manager.get_sbc_by_name("a") is not None

    def test_own_claim_goes_with_the_board(self, lab):
        client, manager = lab
        resp = client.post("/api/claims/a", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 201
        resp = client.delete("/api/sbcs/a", headers=BOT)
        assert resp.status_code == 200
        assert manager.get_sbc_by_name("a") is None

    def test_claims_disabled_still_needs_override(self, tmp_path):
        _, client, manager = _make(tmp_path, enabled=False)
        manager.create_sbc(name="a")
        _claim(manager, "a", owner="bot2")
        resp = client.delete("/api/sbcs/a", headers=OP)
        assert resp.status_code == 409
        resp = client.delete("/api/sbcs/a?override_claim=true", headers=OP)
        assert resp.status_code == 200

    def test_claims_disabled_agent_override_refused(self, tmp_path):
        _, client, manager = _make(tmp_path, enabled=False)
        manager.create_sbc(name="a")
        _claim(manager, "a", owner="bot2")
        resp = client.delete("/api/sbcs/a?override_claim=true", headers=BOT)
        assert resp.status_code == 409
        assert manager.get_sbc_by_name("a") is not None

    def test_own_claim_on_board_shared_with_others_claim(self, lab):
        """Deleting your own claimed board isn't stopped by your claim, nor
        by another user's claim on a board sharing its outlet (deleting
        b's records doesn't touch a)."""
        client, manager = lab
        _claim(manager, "b", owner="bot", session="mcp-http:1-2:bot")
        _claim(manager, "a", owner="bot2", session="mcp-http:1-2:bot2")
        assert client.delete("/api/sbcs/b", headers=BOT).status_code == 200
        assert manager.get_sbc_by_name("b") is None
        assert manager.get_active_claim("a") is not None


# --- REST: the claims endpoints ---


class TestRestClaims:
    def test_claim_records_owner_and_stable_session(self, lab):
        client, manager = lab
        resp = client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 201
        claim = manager.get_active_claim("c")
        assert claim.owner == "bot"
        assert claim.session_id == "web:bot"
        assert claim.session_kind == "web"
        assert claim.agent_name == "bot"
        assert AGENT_KEY[:6] not in claim.session_id

    def test_owner_releases_claim_made_over_mcp(self, lab):
        client, manager = lab
        _claim(manager, "c", owner="bot", session="mcp-http:1-2:bot")
        resp = client.post("/api/claims/c/release", headers=BOT)
        assert resp.status_code == 200
        assert manager.get_active_claim("c") is None

    def test_other_user_cannot_release_or_renew(self, lab):
        client, manager = lab
        client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        assert client.post("/api/claims/c/release", headers=BOT2).status_code == 403
        assert client.post("/api/claims/c/renew", headers=BOT2).status_code == 403
        assert manager.get_active_claim("c") is not None

    def test_renew_bounds_checked(self, lab):
        client, _ = lab
        client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        resp = client.post(
            "/api/claims/c/renew", json={"duration_minutes": 99999}, headers=BOT
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "duration_out_of_bounds"

    @pytest.mark.parametrize("value", ["abc", None, [5], True])
    def test_bad_duration_is_400(self, lab, value):
        client, _ = lab
        resp = client.post(
            "/api/claims/c", json={"duration_minutes": value}, headers=BOT
        )
        assert resp.status_code == 400

    def test_own_active_claim_is_already_claimed(self, lab):
        client, _ = lab
        client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        resp = client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "already_claimed"

    def test_board_sharing_claimed_hardware_refused(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        resp = client.post("/api/claims/b", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["sbc_name"] == "a"
        assert manager.get_active_claim("b") is None
        assert "override_claim" not in " ".join(body["hints"])

    def test_claim_in_grace_period_blocks_others(self, lab):
        client, manager = lab
        claim = _claim(manager, "c", owner="bot2")
        manager.db.execute_modify(
            "UPDATE claims SET expires_at = datetime('now', 'localtime', "
            "'-10 seconds') WHERE id = ?",
            (claim.id,),
        )
        resp = client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 409
        assert "grace" in resp.get_json()["message"]

    def test_agent_cannot_force_release(self, lab):
        client, manager = lab
        _claim(manager, "c", owner="bot2")
        resp = client.post(
            "/api/claims/c/force-release", json={"reason": "x"}, headers=BOT
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "not_operator"
        assert manager.get_active_claim("c") is not None

    def test_operator_force_release_names_operator(self, lab):
        client, manager = lab
        _claim(manager, "c", owner="bot2")
        resp = client.post(
            "/api/claims/c/force-release", json={"reason": "x"}, headers=OP
        )
        assert resp.status_code == 200
        released = manager.list_claim_history("c")[0]
        assert released.released_by == "alice"

    def test_request_release_names_requester(self, lab):
        client, manager = lab
        _claim(manager, "c", owner="bot2")
        resp = client.post(
            "/api/claims/c/request-release", json={"reason": "need it"}, headers=BOT
        )
        assert resp.status_code == 200
        assert manager.get_active_claim("c").pending_requests[0].requested_by == "bot"


# --- Web UI ---


class TestWebUi:
    def test_power_refused_page_offers_override(self, lab, view_power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "alice")
        resp = client.post(
            "/sbc/a/power/on", data={"_csrf_token": _csrf(client, "/sbc/a")}
        )
        assert resp.status_code == 409
        html = resp.data.decode()
        assert "holder" in html
        assert 'name="override_claim" value="1"' in html
        view_power.power_on.assert_not_called()

    def test_override_button_acts_and_logs(self, lab, view_power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "alice")
        resp = client.post(
            "/sbc/a/power/on",
            data={"_csrf_token": _csrf(client, "/sbc/a"), "override_claim": "1"},
        )
        assert resp.status_code == 302
        view_power.power_on.assert_called_once()
        logged = _overrides(manager, "a")
        assert len(logged) == 1
        assert logged[0]["actor"] == "web:alice"

    def test_refused_page_reposts_form_fields(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "alice")
        resp = client.post(
            "/sbc/a/network/set",
            data={
                "_csrf_token": _csrf(client, "/sbc/a"),
                "address_type": "ethernet",
                "ip_address": "10.1.1.1",
            },
        )
        assert resp.status_code == 409
        html = resp.data.decode()
        assert 'name="ip_address" value="10.1.1.1"' in html
        assert 'action="/sbc/a/network/set"' in html

    def test_agent_gets_no_override_button(self, lab, view_power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "bot")
        resp = client.post(
            "/sbc/a/power/on", data={"_csrf_token": _csrf(client, "/sbc/a")}
        )
        assert resp.status_code == 409
        assert "override_claim" not in resp.data.decode()

    def test_agent_override_refused(self, lab, view_power):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "bot")
        resp = client.post(
            "/sbc/a/power/on",
            data={"_csrf_token": _csrf(client, "/sbc/a"), "override_claim": "1"},
        )
        assert resp.status_code == 403
        view_power.power_on.assert_not_called()

    def test_own_rest_claim_counts_in_the_ui(self, lab, view_power):
        client, manager = lab
        assert (
            client.post("/api/claims/a", json={"reason": "r"}, headers=BOT).status_code
            == 201
        )
        _login(client, "bot")
        resp = client.post(
            "/sbc/a/power/on", data={"_csrf_token": _csrf(client, "/sbc/a")}
        )
        assert resp.status_code == 302
        view_power.power_on.assert_called_once()

    def test_agent_cannot_force_release(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "bot")
        resp = client.post(
            "/sbc/a/claim/force-release",
            data={"_csrf_token": _csrf(client, "/sbc/a"), "reason": "x"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Only operators may force-release" in resp.data
        assert manager.get_active_claim("a") is not None

    def test_plug_on_claimed_outlet_refused(self, lab):
        client, manager = lab
        _claim(manager, "a", owner="bot2")
        _login(client, "alice")
        resp = client.post(
            "/sbc/c/plug/assign",
            data={
                "_csrf_token": _csrf(client, "/sbc/c"),
                "plug_type": "tasmota",
                "address": "10.0.0.5",
                "plug_index": "1",
            },
        )
        assert resp.status_code == 409
        assert manager.get_sbc_by_name("c").power_plug.plug_index == 2

    @pytest.mark.parametrize(
        "index", ["0", "-1", "x", "²", "70000", str(10**20), "1" * 5000]
    )
    def test_plug_index_from_one(self, lab, index):
        client, manager = lab
        _login(client, "alice")
        client.post(
            "/sbc/c/plug/assign",
            data={
                "_csrf_token": _csrf(client, "/sbc/c"),
                "plug_type": "tasmota",
                "address": "10.0.0.9",
                "plug_index": index,
            },
        )
        assert manager.get_sbc_by_name("c").power_plug.address == "10.0.0.5"

    @pytest.mark.parametrize(
        "path,data",
        [
            ("/sbc/a/edit", {"name": "a", "project": "x"}),
            ("/sbc/a/port/remove/console", {}),
            ("/sbc/a/network/remove/ethernet", {}),
            (
                "/sbc/a/network/set",
                {"address_type": "ethernet", "ip_address": "10.9.9.9"},
            ),
            ("/sbc/a/plug/remove", {}),
            ("/sbc/a/port/assign", {"port_type": "console", "device": "/dev/x"}),
        ],
    )
    def test_config_forms_refused(self, lab, path, data):
        client, manager = lab
        a = manager.get_sbc_by_name("a")
        manager.assign_serial_port(a.id, PortType.CONSOLE, "/dev/lab/port-a")
        manager.set_network_address(a.id, AddressType.ETHERNET, "10.1.1.1")
        _claim(manager, "a", owner="bot2")
        _login(client, "alice")
        resp = client.post(path, data={"_csrf_token": _csrf(client, "/sbc/a"), **data})
        assert resp.status_code == 409
        a = manager.get_sbc_by_name("a")
        assert a.project is None
        assert a.power_plug is not None
        assert a.console_port.device_path == "/dev/lab/port-a"
        assert [n.ip_address for n in a.network_addresses] == ["10.1.1.1"]

    def test_no_auth_is_one_operator_identity(self, tmp_path, view_power):
        _, client, manager = _make(tmp_path, auth=False)
        sbc = manager.create_sbc(name="a")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.5", 1)
        _claim(manager, "a")
        resp = client.post("/sbc/a/power/on")
        assert resp.status_code == 409
        assert 'name="override_claim"' in resp.data.decode()
        resp = client.post("/sbc/a/power/on", data={"override_claim": "1"})
        assert resp.status_code == 302
        view_power.power_on.assert_called_once()


class TestRaces:
    def test_claim_made_after_check_is_not_force_deleted(self, lab):
        """An operator's override only forces past the claim check() saw."""
        client, manager = lab
        from labctl.web import claims as web_claims

        real_check = web_claims.check

        def check_then_claim(*args, **kwargs):
            result = real_check(*args, **kwargs)
            _claim(manager, "c", owner="bot2")
            return result

        with patch.object(web_claims, "check", check_then_claim):
            resp = client.delete("/api/sbcs/c?override_claim=1", headers=OP)
        assert resp.status_code == 409
        assert manager.get_sbc_by_name("c") is not None
        assert manager.get_active_claim("c") is not None

    @staticmethod
    def _rename_after_load(manager):
        """Rename board 'a' to 'z' (and claim it as bot2) right after a
        handler loads it, before its claim check."""
        from labctl.core.manager import ResourceManager

        real = ResourceManager.get_sbc_by_name
        done = []

        def load_then_rename(self, name):
            sbc = real(self, name)
            if name == "a" and not done:
                done.append(True)
                manager.update_sbc(sbc.id, name="z")
                _claim(manager, "z", owner="bot2")
            return sbc

        return patch.object(ResourceManager, "get_sbc_by_name", load_then_rename)

    def test_rest_board_renamed_after_load_is_refused(self, lab, power):
        client, manager = lab
        with self._rename_after_load(manager):
            resp = client.post("/api/sbcs/a/power", json={"action": "on"}, headers=BOT)
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "sbc_changed"
        power.power_on.assert_not_called()

    def test_web_board_renamed_after_load_is_refused(self, lab, view_power):
        client, manager = lab
        _login(client, "alice")
        token = _csrf(client, "/sbc/a")
        with self._rename_after_load(manager):
            resp = client.post("/sbc/a/power/on", data={"_csrf_token": token})
        assert resp.status_code == 404
        view_power.power_on.assert_not_called()


@pytest.mark.parametrize("minutes", [30.0, 5.0])
def test_whole_float_duration_accepted(lab, minutes):
    client, manager = lab
    resp = client.post("/api/claims/c", json={"duration_minutes": minutes}, headers=BOT)
    assert resp.status_code == 201
    assert resp.get_json()["claim"]["duration_seconds"] == int(minutes) * 60


def test_fractional_duration_refused(lab):
    client, _ = lab
    resp = client.post("/api/claims/c", json={"duration_minutes": 2.5}, headers=BOT)
    assert resp.status_code == 400


class TestRestRequireAgentName:
    """claims.require_agent_name: a REST claim must be named, by the
    authenticated user or by agent_name when auth is off."""

    def test_no_auth_unnamed_refused(self, tmp_path):
        app, client, manager = _make(tmp_path, auth=False, require_agent_name=True)
        manager.create_sbc(name="c")
        resp = client.post("/api/claims/c", json={"reason": "r", "agent_name": " "})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "agent_name_required"
        assert manager.get_active_claim("c") is None

    def test_no_auth_named_allowed(self, tmp_path):
        app, client, manager = _make(tmp_path, auth=False, require_agent_name=True)
        manager.create_sbc(name="c")
        resp = client.post("/api/claims/c", json={"reason": "r", "agent_name": "ci"})
        assert resp.status_code == 201
        assert manager.get_active_claim("c").agent_name == "ci"

    def test_authenticated_user_counts_as_name(self, tmp_path):
        app, client, manager = _make(tmp_path, require_agent_name=True)
        manager.create_sbc(name="c")
        resp = client.post("/api/claims/c", json={"reason": "r"}, headers=BOT)
        assert resp.status_code == 201
        assert manager.get_active_claim("c").agent_name == "bot"

    def test_off_by_default(self, tmp_path):
        app, client, manager = _make(tmp_path, auth=False)
        manager.create_sbc(name="c")
        resp = client.post("/api/claims/c", json={"reason": "r"})
        assert resp.status_code == 201
        assert manager.get_active_claim("c").agent_name == "web-operator"
