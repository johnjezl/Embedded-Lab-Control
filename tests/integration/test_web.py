"""Integration tests for Flask web interface and REST API."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from labctl.web.app import create_app
from labctl.core.config import Config, SerialConfig, Ser2NetConfig
from labctl.core.manager import get_manager
from labctl.core.models import Status, PortType, PlugType
from labctl.power import PowerState


@pytest.fixture
def app(tmp_path):
    """Create Flask app with test configuration."""
    db_path = tmp_path / "test.db"
    config = Config(
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
    )
    app = create_app(config)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def manager(app):
    """Get resource manager for test database."""
    with app.app_context():
        from flask import g
        config = app.config["LABCTL_CONFIG"]
        return get_manager(config.database_path)


@pytest.fixture
def sample_sbc(manager):
    """Create a sample SBC for testing."""
    return manager.create_sbc(
        name="test-pi",
        project="test-project",
        description="Test Raspberry Pi",
        ssh_user="pi",
    )


class TestAppFactory:
    """Tests for Flask application factory."""

    def test_create_app(self, app):
        """Test app is created successfully."""
        assert app is not None
        assert app.config["TESTING"] is True

    def test_app_has_blueprints(self, app):
        """Test app has required blueprints registered."""
        assert "api" in app.blueprints
        assert "views" in app.blueprints

    def test_config_loaded(self, app):
        """Test configuration is loaded."""
        assert "LABCTL_CONFIG" in app.config
        assert app.config["LABCTL_CONFIG"].serial.dev_dir == Path("/dev/lab")


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, client):
        """Test /api/health endpoint."""
        response = client.get("/api/health")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "healthy"
        assert "version" in data

    def test_status_empty(self, client):
        """Test /api/status with no SBCs."""
        response = client.get("/api/status")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["sbcs"] == []
        assert data["count"] == 0


class TestSBCEndpoints:
    """Tests for SBC CRUD endpoints."""

    def test_list_sbcs_empty(self, client):
        """Test listing SBCs when none exist."""
        response = client.get("/api/sbcs")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["sbcs"] == []
        assert data["count"] == 0

    def test_create_sbc(self, client):
        """Test creating an SBC via API."""
        response = client.post(
            "/api/sbcs",
            data=json.dumps({
                "name": "api-test-sbc",
                "project": "api-test",
                "description": "Created via API",
            }),
            content_type="application/json",
        )
        assert response.status_code == 201

        data = json.loads(response.data)
        assert data["name"] == "api-test-sbc"
        assert data["project"] == "api-test"
        assert data["description"] == "Created via API"

    def test_create_sbc_missing_name(self, client):
        """Test creating SBC without name fails."""
        response = client.post(
            "/api/sbcs",
            data=json.dumps({"project": "test"}),
            content_type="application/json",
        )
        assert response.status_code == 400

        data = json.loads(response.data)
        assert "error" in data
        assert "name" in data["error"]

    def test_create_sbc_duplicate_name(self, client, sample_sbc):
        """Test creating SBC with duplicate name fails."""
        response = client.post(
            "/api/sbcs",
            data=json.dumps({"name": "test-pi"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_get_sbc(self, client, sample_sbc):
        """Test getting SBC by name."""
        response = client.get("/api/sbcs/test-pi")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["name"] == "test-pi"
        assert data["project"] == "test-project"
        assert data["ssh_user"] == "pi"

    def test_get_sbc_not_found(self, client):
        """Test getting non-existent SBC returns 404."""
        response = client.get("/api/sbcs/nonexistent")
        assert response.status_code == 404

        data = json.loads(response.data)
        assert "error" in data

    def test_update_sbc(self, client, sample_sbc):
        """Test updating an SBC."""
        response = client.put(
            "/api/sbcs/test-pi",
            data=json.dumps({
                "project": "updated-project",
                "description": "Updated description",
            }),
            content_type="application/json",
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["project"] == "updated-project"
        assert data["description"] == "Updated description"

    def test_update_sbc_status(self, client, sample_sbc):
        """Test updating SBC status."""
        response = client.put(
            "/api/sbcs/test-pi",
            data=json.dumps({"status": "online"}),
            content_type="application/json",
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "online"

    def test_update_sbc_invalid_status(self, client, sample_sbc):
        """Test updating with invalid status fails."""
        response = client.put(
            "/api/sbcs/test-pi",
            data=json.dumps({"status": "invalid-status"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_delete_sbc(self, client, sample_sbc):
        """Test deleting an SBC."""
        response = client.delete("/api/sbcs/test-pi")
        assert response.status_code == 200

        # Verify it's gone
        response = client.get("/api/sbcs/test-pi")
        assert response.status_code == 404

    def test_delete_sbc_not_found(self, client):
        """Test deleting non-existent SBC returns 404."""
        response = client.delete("/api/sbcs/nonexistent")
        assert response.status_code == 404

    def test_list_sbcs_with_filter(self, client, manager):
        """Test listing SBCs with project filter."""
        manager.create_sbc(name="sbc1", project="proj-a")
        manager.create_sbc(name="sbc2", project="proj-b")
        manager.create_sbc(name="sbc3", project="proj-a")

        response = client.get("/api/sbcs?project=proj-a")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["count"] == 2
        assert all(s["project"] == "proj-a" for s in data["sbcs"])


class TestPortEndpoints:
    """Tests for serial port endpoints."""

    def test_list_ports_empty(self, client):
        """Test listing ports when none assigned."""
        response = client.get("/api/ports")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["ports"] == []
        assert data["count"] == 0

    def test_list_ports_with_assignments(self, client, manager, sample_sbc):
        """Test listing ports with assignments."""
        manager.assign_serial_port(
            sbc_id=sample_sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/test-pi",
            tcp_port=4000,
        )

        response = client.get("/api/ports")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["count"] == 1
        assert data["ports"][0]["sbc_name"] == "test-pi"
        assert data["ports"][0]["device"] == "/dev/lab/test-pi"

    def test_assign_port_via_api(self, client, sample_sbc):
        """Test assigning port via API."""
        response = client.post(
            "/api/sbcs/test-pi/ports",
            data=json.dumps({
                "type": "console",
                "device": "/dev/lab/new-device",
                "baud_rate": 9600,
            }),
            content_type="application/json",
        )
        assert response.status_code == 201

        data = json.loads(response.data)
        assert data["type"] == "console"
        assert data["device"] == "/dev/lab/new-device"
        assert data["baud_rate"] == 9600

    def test_assign_port_invalid_type(self, client, sample_sbc):
        """Test assigning port with invalid type fails."""
        response = client.post(
            "/api/sbcs/test-pi/ports",
            data=json.dumps({
                "type": "invalid-type",
                "device": "/dev/lab/test",
            }),
            content_type="application/json",
        )
        assert response.status_code == 400


class TestPowerEndpoints:
    """Tests for power control endpoints."""

    def test_get_power_no_plug(self, client, sample_sbc):
        """Test getting power status with no plug assigned."""
        response = client.get("/api/sbcs/test-pi/power")
        assert response.status_code == 400

        data = json.loads(response.data)
        assert "No power plug" in data["error"]

    def test_power_action_no_plug(self, client, sample_sbc):
        """Test power action with no plug assigned."""
        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({"action": "on"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_power_action_missing_action(self, client, manager, sample_sbc):
        """Test power action without action field fails."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert response.status_code == 400

        data = json.loads(response.data)
        assert "action is required" in data["error"]

    def test_power_action_invalid_action(self, client, manager, sample_sbc):
        """Test power action with invalid action fails."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({"action": "invalid"}),
            content_type="application/json",
        )
        assert response.status_code == 400

        data = json.loads(response.data)
        assert "must be on, off, or cycle" in data["error"]

    @patch("labctl.web.api.PowerController")
    def test_power_on(self, mock_power, client, manager, sample_sbc):
        """Test power on action."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        mock_controller = MagicMock()
        mock_controller.power_on.return_value = True
        mock_controller.get_state.return_value = PowerState.ON
        mock_power.from_plug.return_value = mock_controller

        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({"action": "on"}),
            content_type="application/json",
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["success"] is True
        assert data["action"] == "on"
        assert data["state"] == "on"

    @patch("labctl.web.api.PowerController")
    def test_power_off(self, mock_power, client, manager, sample_sbc):
        """Test power off action."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        mock_controller = MagicMock()
        mock_controller.power_off.return_value = True
        mock_controller.get_state.return_value = PowerState.OFF
        mock_power.from_plug.return_value = mock_controller

        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({"action": "off"}),
            content_type="application/json",
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["success"] is True
        assert data["action"] == "off"

    @patch("labctl.web.api.PowerController")
    def test_power_cycle(self, mock_power, client, manager, sample_sbc):
        """Test power cycle action."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        mock_controller = MagicMock()
        mock_controller.power_cycle.return_value = True
        mock_controller.get_state.return_value = PowerState.ON
        mock_power.from_plug.return_value = mock_controller

        response = client.post(
            "/api/sbcs/test-pi/power",
            data=json.dumps({"action": "cycle", "delay": 3.0}),
            content_type="application/json",
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["success"] is True
        assert data["action"] == "cycle"

    @patch("labctl.web.api.PowerController")
    def test_get_power_status(self, mock_power, client, manager, sample_sbc):
        """Test getting power status."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        mock_controller = MagicMock()
        mock_controller.get_state.return_value = PowerState.ON
        mock_power.from_plug.return_value = mock_controller

        response = client.get("/api/sbcs/test-pi/power")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["state"] == "on"
        assert data["plug_type"] == "tasmota"


class TestDashboardViews:
    """Tests for dashboard HTML views."""

    def test_dashboard_empty(self, client):
        """Test dashboard with no SBCs."""
        response = client.get("/")
        assert response.status_code == 200
        assert b"Lab Controller" in response.data
        assert b"No SBCs configured" in response.data

    def test_dashboard_with_sbcs(self, client, sample_sbc):
        """Test dashboard displays SBCs."""
        response = client.get("/")
        assert response.status_code == 200
        assert b"test-pi" in response.data
        assert b"test-project" in response.data

    def test_sbc_detail_page(self, client, sample_sbc):
        """Test SBC detail page."""
        response = client.get("/sbc/test-pi")
        assert response.status_code == 200
        assert b"test-pi" in response.data
        assert b"Test Raspberry Pi" in response.data

    def test_sbc_detail_not_found(self, client):
        """Test SBC detail page for non-existent SBC redirects."""
        response = client.get("/sbc/nonexistent", follow_redirects=True)
        assert response.status_code == 200
        # Should redirect to dashboard with flash message
        assert b"not found" in response.data

    @patch("labctl.web.views.PowerController")
    def test_power_action_view(self, mock_power, client, manager, sample_sbc):
        """Test power action via view."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        mock_controller = MagicMock()
        mock_controller.power_on.return_value = True
        mock_power.from_plug.return_value = mock_controller

        response = client.post(
            "/sbc/test-pi/power/on",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Power ON" in response.data

    def test_power_action_no_plug_view(self, client, sample_sbc):
        """Test power action via view with no plug."""
        response = client.post(
            "/sbc/test-pi/power/on",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"No power plug" in response.data

    def test_power_action_unknown_action(self, client, manager, sample_sbc):
        """Test power action with unknown action."""
        manager.assign_power_plug(
            sample_sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.100",
        )

        response = client.post(
            "/sbc/test-pi/power/invalid",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Unknown action" in response.data
