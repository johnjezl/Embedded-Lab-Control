"""MCP HTTP transport security (D012 WS4): API-key bearer auth, refusing
unauthenticated non-loopback binds, and Host/Origin header checks."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from labctl.core.config import AuthConfig, Config, UserConfig

ALICE_KEY = "alice-key-0123456789abcdef"
BOB_KEY = "bob-key-0123456789abcdefgh"


def _config(enabled=True, users=None, allowed_hosts=None, allowed_origins=None):
    config = Config()
    config.auth = AuthConfig(
        enabled=enabled,
        users=(
            users
            if users is not None
            else [
                UserConfig(username="alice", api_key=ALICE_KEY),
                UserConfig(username="bob", api_key=BOB_KEY),
            ]
        ),
    )
    config.mcp.allowed_hosts = list(allowed_hosts or [])
    config.mcp.allowed_origins = list(allowed_origins or [])
    return config


class TestUserForApiKey:
    def test_matches_user(self):
        assert _config().auth.user_for_api_key(BOB_KEY).username == "bob"

    def test_wrong_and_empty_keys_never_match(self):
        auth = _config(users=[UserConfig(username="nokey", api_key="")]).auth
        assert auth.user_for_api_key("") is None
        assert auth.user_for_api_key("x") is None
        assert _config().auth.user_for_api_key(ALICE_KEY + "x") is None

    def test_non_ascii_key_does_not_raise(self):
        # hmac.compare_digest raises TypeError on non-ASCII str.
        assert _config().auth.user_for_api_key("kéy\udcff") is None

    def test_non_string_configured_key_is_skipped(self):
        auth = _config(users=[UserConfig(username="num", api_key=12345)]).auth
        assert auth.user_for_api_key("12345") is None

    @pytest.mark.parametrize("username", [1001, "", None])
    def test_user_without_usable_name_never_matches(self, username):
        """A YAML `username: 1001` loads as int (the SDK rejects it as a
        client_id); a blank one would look unauthenticated."""
        auth = _config(users=[UserConfig(username=username, api_key=ALICE_KEY)]).auth
        assert auth.user_for_api_key(ALICE_KEY) is None
        assert auth.key_users() == []

    def test_web_lookup_delegates(self):
        from labctl.web.auth import get_user_by_api_key

        assert get_user_by_api_key(_config().auth, ALICE_KEY).username == "alice"
        assert get_user_by_api_key(_config().auth, "kéy") is None


class TestConfigParsing:
    def test_allowed_hosts_and_origins(self):
        config = Config.from_dict(
            {
                "mcp": {
                    "allowed_hosts": ["lab.example.com", "lab.example.com:*"],
                    "allowed_origins": "https://lab.example.com",
                }
            }
        )
        assert config.mcp.allowed_hosts == ["lab.example.com", "lab.example.com:*"]
        assert config.mcp.allowed_origins == ["https://lab.example.com"]
        round_trip = config.to_dict()["mcp"]
        assert round_trip["allowed_hosts"] == config.mcp.allowed_hosts
        assert round_trip["allowed_origins"] == config.mcp.allowed_origins

    def test_defaults_empty(self):
        config = Config.from_dict({})
        assert config.mcp.allowed_hosts == []
        assert config.mcp.allowed_origins == []


class TestIsLoopback:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"]
    )
    def test_loopback(self, host):
        from labctl.mcp_server import _is_loopback

        assert _is_loopback(host)

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "::", "192.168.1.5", "lab.example.com", "localhost.evil"]
    )
    def test_not_loopback(self, host):
        from labctl.mcp_server import _is_loopback

        assert not _is_loopback(host)


class TestHttpAuthPolicy:
    def test_auth_enabled_requires_keys(self):
        from labctl.mcp_server import _http_auth_required

        assert _http_auth_required("0.0.0.0", _config()) is True
        assert _http_auth_required("127.0.0.1", _config()) is True

    def test_loopback_without_auth_is_allowed(self):
        from labctl.mcp_server import _http_auth_required

        assert _http_auth_required("127.0.0.1", _config(enabled=False)) is False

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "lab"])
    def test_non_loopback_without_auth_refused(self, host):
        from labctl.mcp_server import McpStartupError, _http_auth_required

        with pytest.raises(McpStartupError, match="without authentication"):
            _http_auth_required(host, _config(enabled=False))

    def test_auth_enabled_without_any_key_refused(self):
        from labctl.mcp_server import McpStartupError, _http_auth_required

        config = _config(users=[UserConfig(username="web-only", password_hash="h")])
        with pytest.raises(McpStartupError, match="no user in auth.users has an"):
            _http_auth_required("127.0.0.1", config)

    @pytest.mark.parametrize(
        "hosts,origins",
        [
            (["lab.example.com"], []),
            (["lab.example.com:*"], []),
            (["10.0.0.5:8080"], []),
            ([], ["https://lab.example.com"]),
        ],
    )
    def test_proxy_names_without_auth_refused(self, hosts, origins):
        """Loopback bind, but accepting a public name means a proxy can
        expose it: refuse without auth."""
        from labctl.mcp_server import McpStartupError, _http_auth_required

        config = _config(enabled=False, allowed_hosts=hosts, allowed_origins=origins)
        with pytest.raises(McpStartupError, match="would expose"):
            _http_auth_required("127.0.0.1", config)

    def test_loopback_names_without_auth_allowed(self):
        from labctl.mcp_server import _http_auth_required

        config = _config(
            enabled=False,
            allowed_hosts=["localhost:*", "127.0.0.1:8080", "[::1]:*"],
            allowed_origins=["http://localhost:3000"],
        )
        assert _http_auth_required("127.0.0.1", config) is False

    def test_proxy_names_with_auth_allowed(self):
        from labctl.mcp_server import _http_auth_required

        assert _http_auth_required("127.0.0.1", _config(allowed_hosts=["lab"])) is True

    def test_only_unusable_usernames_refused(self, caplog):
        from labctl.mcp_server import McpStartupError, _http_auth_required

        config = _config(users=[UserConfig(username=1001, api_key=ALICE_KEY)])
        with pytest.raises(McpStartupError, match="api_key and a username"):
            _http_auth_required("127.0.0.1", config)
        assert "quote numeric usernames" in caplog.text

    def test_short_key_warns(self, caplog):
        from labctl.mcp_server import _http_auth_required

        config = _config(users=[UserConfig(username="weak", api_key="short")])
        assert _http_auth_required("127.0.0.1", config) is True
        assert "shorter than" in caplog.text


class TestTransportSecurity:
    def test_loopback_defaults(self):
        from labctl.mcp_server import _transport_security

        s = _transport_security("127.0.0.1", _config().mcp)
        assert s.enable_dns_rebinding_protection
        assert "127.0.0.1:*" in s.allowed_hosts
        assert "localhost:*" in s.allowed_hosts
        assert "[::1]:*" in s.allowed_hosts

    def test_other_loopback_bind_is_accepted(self):
        from labctl.mcp_server import _transport_security

        assert (
            "127.0.0.2:*"
            in _transport_security("127.0.0.2", _config().mcp).allowed_hosts
        )

    def test_configured_hosts_extend_loopback(self):
        from labctl.mcp_server import _transport_security

        mcp_cfg = _config(
            allowed_hosts=["lab.example.com"],
            allowed_origins=["https://lab.example.com"],
        ).mcp
        s = _transport_security("127.0.0.1", mcp_cfg)
        assert "lab.example.com" in s.allowed_hosts
        assert "127.0.0.1:*" in s.allowed_hosts
        assert "https://lab.example.com" in s.allowed_origins

    def test_non_loopback_with_hosts(self):
        from labctl.mcp_server import _transport_security

        s = _transport_security("0.0.0.0", _config(allowed_hosts=["lab:*"]).mcp)
        assert s.enable_dns_rebinding_protection
        assert s.allowed_hosts == ["lab:*"]

    def test_non_loopback_without_hosts_warns(self, caplog):
        from labctl.mcp_server import _transport_security

        assert _transport_security("0.0.0.0", _config().mcp) is None
        assert "allowed_hosts is not set" in caplog.text
        assert "allowed_origins is ignored" not in caplog.text

    def test_non_loopback_origins_without_hosts_warns_ignored(self, caplog):
        from labctl.mcp_server import _transport_security

        mcp_cfg = _config(allowed_origins=["https://lab.example.com"]).mcp
        assert _transport_security("0.0.0.0", mcp_cfg) is None
        assert "allowed_origins is ignored without allowed_hosts" in caplog.text


class TestRunServerRefuses:
    def test_refuses_before_starting_anything(self):
        from labctl import mcp_server

        with (
            patch.object(
                mcp_server, "_get_config", return_value=_config(enabled=False)
            ),
            patch.object(mcp_server, "_start_expiry_thread") as expiry,
            patch.object(mcp_server.mcp, "run") as run,
        ):
            with pytest.raises(mcp_server.McpStartupError):
                mcp_server.run_server(transport="http", host="0.0.0.0")
        expiry.assert_not_called()
        run.assert_not_called()

    def test_cli_reports_refusal(self, tmp_path):
        from click.testing import CliRunner

        from labctl.cli import main

        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"database_path: {tmp_path / 'x.db'}\n")
        with patch("labctl.mcp_server.mcp.run") as run:
            result = CliRunner().invoke(
                main, ["-c", str(cfg), "mcp", "--http", "8080", "--host", "0.0.0.0"]
            )
        assert result.exit_code != 0
        assert "without authentication" in result.output
        run.assert_not_called()

    def test_passes_security_and_enables_auth(self, http_auth_restore):
        from labctl import mcp_server

        with (
            patch.object(mcp_server, "_get_config", return_value=_config()),
            patch.object(mcp_server, "_start_expiry_thread"),
            patch("atexit.register"),
            patch.object(mcp_server.mcp, "run") as run,
        ):
            mcp_server.run_server(transport="http", host="127.0.0.1", http_port=9)
        kwargs = run.call_args.kwargs
        assert kwargs["transport_security"].enable_dns_rebinding_protection
        assert isinstance(mcp_server.mcp._token_verifier, mcp_server._ApiKeyVerifier)


# ---------------------------------------------------------------------------
# Real HTTP requests against the SDK app: pins how _enable_http_auth plugs
# into the SDK (it sets attributes the SDK reads when building the app).
# ---------------------------------------------------------------------------


@pytest.fixture
def http_auth_restore():
    from labctl import mcp_server

    saved = (mcp_server.mcp.settings.auth, mcp_server.mcp._token_verifier)
    yield
    mcp_server.mcp.settings.auth, mcp_server.mcp._token_verifier = saved


@pytest.fixture
def whoami_tool():
    """A throwaway tool reporting the audit actor its call runs under."""
    from labctl import mcp_server
    from labctl.core import audit

    @mcp_server._with_mcp_activity
    def whoami() -> str:
        return audit.current_actor()

    mcp_server.mcp.add_tool(whoami, name="test_whoami")
    yield
    mcp_server.mcp.remove_tool("test_whoami")


@pytest.fixture
def live_server(http_auth_restore, whoami_tool):
    """Serve the MCP app on an ephemeral loopback port with auth enabled."""
    import uvicorn

    from labctl import mcp_server

    config = _config(allowed_hosts=["lab.example.com"])
    with patch.object(mcp_server, "_get_config", return_value=config):
        mcp_server._enable_http_auth()
        app = mcp_server.mcp.streamable_http_app(
            transport_security=mcp_server._transport_security("127.0.0.1", config.mcp)
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "MCP HTTP server did not start"
            time.sleep(0.02)
        try:
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _post(url, body, token=None, session=None, host=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    if host:
        headers["Host"] = host
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def _open_session(url, token):
    status, headers, _ = _post(url, INIT, token)
    assert status == 200
    session = headers["Mcp-Session-Id"]
    notified = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert _post(url, notified, token, session)[0] == 202
    return session


CALL_WHOAMI = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "test_whoami", "arguments": {}},
}


class TestLiveHttpAuth:
    def test_missing_token_rejected(self, live_server):
        status, headers, _ = _post(live_server, INIT)
        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Bearer")

    @pytest.mark.parametrize("token", ["wrong", "", ALICE_KEY[:-1], "kéy"])
    def test_bad_token_rejected(self, live_server, token):
        assert _post(live_server, INIT, token)[0] == 401

    def test_valid_key_attributes_audit_to_user(self, live_server):
        session = _open_session(live_server, ALICE_KEY)
        status, _, body = _post(live_server, CALL_WHOAMI, ALICE_KEY, session)
        assert status == 200
        assert ":alice" in body

    def test_session_bound_to_its_user(self, live_server):
        session = _open_session(live_server, ALICE_KEY)
        status, _, body = _post(live_server, CALL_WHOAMI, BOB_KEY, session)
        assert status in (403, 404)
        assert ":alice" not in body

    def test_revoked_key_rejected_without_restart(self, live_server):
        from labctl import mcp_server

        session = _open_session(live_server, ALICE_KEY)
        revoked = _config(users=[UserConfig(username="bob", api_key=BOB_KEY)])
        with patch.object(mcp_server, "_get_config", return_value=revoked):
            assert _post(live_server, CALL_WHOAMI, ALICE_KEY, session)[0] == 401

    def test_unknown_host_header_rejected(self, live_server):
        assert _post(live_server, INIT, ALICE_KEY, host="evil.example:80")[0] == 421

    def test_configured_host_header_accepted(self, live_server):
        assert _post(live_server, INIT, ALICE_KEY, host="lab.example.com")[0] == 200
