"""Integration tests for authentication and authorization."""

from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from labctl.core.config import (
    AuthConfig,
    Config,
    Ser2NetConfig,
    SerialConfig,
    UserConfig,
)
from labctl.web.app import create_app

TEST_PASSWORD = "testpass123"
TEST_API_KEY = "test-api-key-abc123"


@pytest.fixture
def auth_config(tmp_path):
    """Create a Config with auth enabled and a test user."""
    db_path = tmp_path / "test.db"
    return Config(
        database_path=db_path,
        serial=SerialConfig(
            dev_dir=Path("/dev/lab"),
            base_tcp_port=4000,
            default_baud=115200,
        ),
        ser2net=Ser2NetConfig(
            config_file=tmp_path / "ser2net.yaml",
            enabled=True,
        ),
        auth=AuthConfig(
            enabled=True,
            secret_key="test-secret-key",
            users=[
                UserConfig(
                    username="admin",
                    password_hash=generate_password_hash(TEST_PASSWORD),
                    api_key=TEST_API_KEY,
                ),
            ],
        ),
    )


@pytest.fixture
def auth_app(auth_config):
    """Create Flask app with auth enabled."""
    app = create_app(auth_config)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def auth_client(auth_app):
    """Create test client for auth-enabled app."""
    return auth_app.test_client()


def _login(client, username="admin", password=TEST_PASSWORD):
    """Helper to log in and return response."""
    # Get CSRF token from login page
    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.data.decode()
    # Extract CSRF token from form
    import re

    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    csrf_token = match.group(1) if match else ""
    return client.post(
        "/login",
        data={"username": username, "password": password, "_csrf_token": csrf_token},
        follow_redirects=False,
    )


class TestLoginLogout:
    """Test login and logout flows."""

    def test_login_page_accessible(self, auth_client):
        resp = auth_client.get("/login")
        assert resp.status_code == 200
        assert b"Sign In" in resp.data

    def test_login_success(self, auth_client):
        resp = _login(auth_client)
        assert resp.status_code == 302
        # Should redirect to dashboard
        assert resp.headers["Location"].endswith("/")

    def test_login_wrong_password(self, auth_client):
        resp = _login(auth_client, password="wrongpass")
        # Should stay on login page (redirect back or re-render)
        assert resp.status_code == 200 or resp.status_code == 302

    def test_login_unknown_user(self, auth_client):
        resp = _login(auth_client, username="nobody", password="wrongpass")
        assert resp.status_code == 200 or resp.status_code == 302

    def test_logout(self, auth_client):
        # Log in first
        _login(auth_client)

        # Get a CSRF token from a page
        resp = auth_client.get("/")
        import re

        html = resp.data.decode()
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
        csrf_token = match.group(1) if match else ""

        resp = auth_client.post(
            "/logout",
            data={"_csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        # Should not be able to access dashboard after logout
        resp = auth_client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


class TestWebAuthEnforcement:
    """Test that web routes require authentication."""

    def test_dashboard_redirects_to_login(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_settings_redirects_to_login(self, auth_client):
        resp = auth_client.get("/settings")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_dashboard_accessible_after_login(self, auth_client):
        _login(auth_client)
        resp = auth_client.get("/")
        assert resp.status_code == 200


class TestApiKeyAuth:
    """Test API key authentication."""

    def test_api_requires_key(self, auth_client):
        resp = auth_client.get("/api/sbcs")
        assert resp.status_code == 401
        data = resp.get_json()
        assert "API key required" in data["error"]

    def test_api_invalid_key(self, auth_client):
        resp = auth_client.get("/api/sbcs", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        data = resp.get_json()
        assert "Invalid API key" in data["error"]

    def test_api_valid_key(self, auth_client):
        resp = auth_client.get("/api/sbcs", headers={"X-API-Key": TEST_API_KEY})
        assert resp.status_code == 200

    def test_api_post_with_key(self, auth_client):
        resp = auth_client.post(
            "/api/sbcs",
            json={"name": "test-sbc"},
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 201


class TestHealthEndpointOpen:
    """Test that /api/health remains open without auth."""

    def test_health_no_auth(self, auth_client):
        resp = auth_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"


class TestCsrfEnforcement:
    """Test CSRF token enforcement on web POST requests."""

    def test_post_without_csrf_redirects(self, auth_client):
        _login(auth_client)
        # POST without CSRF token should redirect
        resp = auth_client.post("/logout", follow_redirects=False)
        assert resp.status_code == 302


class TestAuthDisabledByDefault:
    """Test that auth is disabled by default — existing behavior unchanged."""

    @pytest.fixture
    def noauth_app(self, tmp_path):
        db_path = tmp_path / "test.db"
        config = Config(
            database_path=db_path,
            serial=SerialConfig(dev_dir=Path("/dev/lab")),
            ser2net=Ser2NetConfig(config_file=tmp_path / "ser2net.yaml"),
        )
        app = create_app(config)
        app.config["TESTING"] = True
        return app

    @pytest.fixture
    def noauth_client(self, noauth_app):
        return noauth_app.test_client()

    def test_dashboard_no_auth(self, noauth_client):
        resp = noauth_client.get("/")
        assert resp.status_code == 200

    def test_api_no_auth(self, noauth_client):
        resp = noauth_client.get("/api/sbcs")
        assert resp.status_code == 200

    def test_post_no_csrf_needed(self, noauth_client):
        resp = noauth_client.post("/api/sbcs", json={"name": "test"})
        assert resp.status_code == 201


class TestOpenRedirectProtection:
    """Test that login redirect prevents open redirect attacks."""

    def _login_with_next(self, client, next_url):
        """Login and redirect to the given next URL."""
        import re

        resp = client.get(f"/login?next={next_url}")
        html = resp.data.decode()
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
        csrf_token = match.group(1) if match else ""
        return client.post(
            f"/login?next={next_url}",
            data={
                "username": "admin",
                "password": TEST_PASSWORD,
                "_csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

    def test_valid_relative_redirect(self, auth_client):
        """Login with valid relative next URL should redirect there."""
        resp = self._login_with_next(auth_client, "/settings")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/settings")

    def test_blocks_absolute_url(self, auth_client):
        """Login with absolute URL should redirect to index instead."""
        resp = self._login_with_next(auth_client, "https://evil.com")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "evil.com" not in location

    def test_blocks_protocol_relative_url(self, auth_client):
        """Login with //evil.com should redirect to index instead."""
        resp = self._login_with_next(auth_client, "//evil.com")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "evil.com" not in location

    def test_empty_next_defaults_to_index(self, auth_client):
        """Login with empty next should redirect to index."""
        resp = self._login_with_next(auth_client, "")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")
