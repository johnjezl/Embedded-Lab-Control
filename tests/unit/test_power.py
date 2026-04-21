"""Unit tests for power control module."""

import asyncio
import logging
import types
import sys
from unittest.mock import Mock, patch

import pytest

from labctl.core.models import PlugType, PowerPlug
from labctl.power.base import PowerController, PowerState, get_controller
from labctl.power.shelly import ShellyController
from labctl.power.tasmota import TasmotaController


class TestPowerState:
    """Tests for PowerState enum."""

    def test_power_states(self):
        """Test power state values."""
        assert PowerState.ON.value == "on"
        assert PowerState.OFF.value == "off"
        assert PowerState.UNKNOWN.value == "unknown"


class TestGetController:
    """Tests for controller factory function."""

    def test_get_tasmota_controller(self):
        """Test creating Tasmota controller."""
        controller = get_controller(PlugType.TASMOTA, "192.168.1.50")
        assert isinstance(controller, TasmotaController)
        assert controller.address == "192.168.1.50"
        assert controller.plug_index == 1

    def test_get_shelly_controller(self):
        """Test creating Shelly controller."""
        controller = get_controller(PlugType.SHELLY, "192.168.1.51", plug_index=2)
        assert isinstance(controller, ShellyController)
        assert controller.address == "192.168.1.51"
        assert controller.plug_index == 2

    def test_get_controller_with_timeout(self):
        """Test creating controller with custom timeout."""
        controller = get_controller(PlugType.TASMOTA, "192.168.1.50", timeout=10.0)
        assert controller.timeout == 10.0


class TestPowerControllerFromPlug:
    """Tests for PowerController.from_plug class method."""

    def test_from_plug_tasmota(self):
        """Test creating controller from PowerPlug model."""
        plug = PowerPlug(
            id=1,
            sbc_id=1,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.50",
            plug_index=1,
        )
        controller = PowerController.from_plug(plug)
        assert isinstance(controller, TasmotaController)
        assert controller.address == "192.168.1.50"

    def test_from_plug_with_index(self):
        """Test creating controller with plug index."""
        plug = PowerPlug(
            id=1,
            sbc_id=1,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.50",
            plug_index=3,
        )
        controller = PowerController.from_plug(plug)
        assert controller.plug_index == 3


class TestTasmotaController:
    """Tests for Tasmota power controller."""

    @patch("labctl.power.tasmota.requests.get")
    def test_power_on_success(self, mock_get):
        """Test successful power on."""
        mock_response = Mock()
        mock_response.json.return_value = {"POWER": "ON"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = TasmotaController("192.168.1.50")
        result = controller.power_on()

        assert result is True
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "192.168.1.50" in call_args[0][0]
        assert call_args[1]["params"]["cmnd"] == "Power1 On"

    @patch("labctl.power.tasmota.requests.get")
    def test_power_off_success(self, mock_get):
        """Test successful power off."""
        mock_response = Mock()
        mock_response.json.return_value = {"POWER": "OFF"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = TasmotaController("192.168.1.50")
        result = controller.power_off()

        assert result is True

    @patch("labctl.power.tasmota.requests.get")
    def test_get_state_on(self, mock_get):
        """Test getting ON state."""
        mock_response = Mock()
        mock_response.json.return_value = {"POWER": "ON"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = TasmotaController("192.168.1.50")
        state = controller.get_state()

        assert state == PowerState.ON

    @patch("labctl.power.tasmota.requests.get")
    def test_get_state_off(self, mock_get):
        """Test getting OFF state."""
        mock_response = Mock()
        mock_response.json.return_value = {"POWER": "OFF"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = TasmotaController("192.168.1.50")
        state = controller.get_state()

        assert state == PowerState.OFF

    @patch("labctl.power.tasmota.requests.get")
    def test_get_state_unknown_on_error(self, mock_get):
        """Test getting UNKNOWN state on connection error."""
        mock_get.side_effect = Exception("Connection failed")

        controller = TasmotaController("192.168.1.50")
        state = controller.get_state()

        assert state == PowerState.UNKNOWN

    @patch("labctl.power.tasmota.requests.get")
    def test_multi_relay_device(self, mock_get):
        """Test controlling specific relay on multi-relay device."""
        mock_response = Mock()
        mock_response.json.return_value = {"POWER2": "ON"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = TasmotaController("192.168.1.50", plug_index=2)
        result = controller.power_on()

        assert result is True
        call_args = mock_get.call_args
        assert call_args[1]["params"]["cmnd"] == "Power2 On"

    @patch("labctl.power.tasmota.requests.get")
    def test_power_on_failure(self, mock_get):
        """Test power on returns False on failure."""
        mock_get.return_value = None
        mock_get.side_effect = Exception("Connection failed")

        controller = TasmotaController("192.168.1.50")
        result = controller.power_on()

        assert result is False

    @patch("labctl.power.tasmota.requests.get")
    def test_failure_logs_warning(self, mock_get, caplog):
        """Test that connection failure logs a warning."""
        mock_get.side_effect = ConnectionError("Network unreachable")

        controller = TasmotaController("192.168.1.50")
        with caplog.at_level(logging.WARNING, logger="labctl.power.tasmota"):
            controller.power_on()

        assert any("Tasmota command" in r.message for r in caplog.records)
        assert any("192.168.1.50" in r.message for r in caplog.records)


class TestShellyController:
    """Tests for Shelly power controller."""

    @patch("labctl.power.shelly.requests.get")
    def test_power_on_success(self, mock_get):
        """Test successful power on."""
        mock_response = Mock()
        mock_response.json.return_value = {"ison": True}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = ShellyController("192.168.1.51")
        result = controller.power_on()

        assert result is True
        call_args = mock_get.call_args
        assert "relay/0" in call_args[0][0]
        assert call_args[1]["params"]["turn"] == "on"

    @patch("labctl.power.shelly.requests.get")
    def test_power_off_success(self, mock_get):
        """Test successful power off."""
        mock_response = Mock()
        mock_response.json.return_value = {"ison": False}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = ShellyController("192.168.1.51")
        result = controller.power_off()

        assert result is True

    @patch("labctl.power.shelly.requests.get")
    def test_get_state_on(self, mock_get):
        """Test getting ON state."""
        mock_response = Mock()
        mock_response.json.return_value = {"ison": True}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        controller = ShellyController("192.168.1.51")
        state = controller.get_state()

        assert state == PowerState.ON

    @patch("labctl.power.shelly.requests.get")
    def test_relay_index_conversion(self, mock_get):
        """Test 1-based to 0-based relay index conversion."""
        mock_response = Mock()
        mock_response.json.return_value = {"ison": True}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # plug_index=2 should become relay/1
        controller = ShellyController("192.168.1.51", plug_index=2)
        controller.power_on()

        call_args = mock_get.call_args
        assert "relay/1" in call_args[0][0]

    @patch("labctl.power.shelly.requests.get")
    def test_failure_logs_warning(self, mock_get, caplog):
        """Test that connection failure logs a warning."""
        mock_get.side_effect = ConnectionError("Network unreachable")

        controller = ShellyController("192.168.1.51")
        with caplog.at_level(logging.WARNING, logger="labctl.power.shelly"):
            controller.power_on()

        assert any("Shelly request" in r.message for r in caplog.records)
        assert any("192.168.1.51" in r.message for r in caplog.records)

    @patch("labctl.power.shelly.requests.get")
    def test_power_on_failure(self, mock_get):
        """Test power on returns False on failure."""
        mock_get.side_effect = Exception("Connection failed")

        controller = ShellyController("192.168.1.51")
        result = controller.power_on()

        assert result is False


class TestPowerCycle:
    """Tests for power cycle functionality."""

    @patch("labctl.power.tasmota.requests.get")
    @patch("time.sleep")
    def test_power_cycle_success(self, mock_sleep, mock_get):
        """Test successful power cycle."""
        mock_response_off = Mock()
        mock_response_off.json.return_value = {"POWER": "OFF"}
        mock_response_off.raise_for_status = Mock()

        mock_response_on = Mock()
        mock_response_on.json.return_value = {"POWER": "ON"}
        mock_response_on.raise_for_status = Mock()

        mock_get.side_effect = [mock_response_off, mock_response_on]

        controller = TasmotaController("192.168.1.50")
        result = controller.power_cycle(delay=3.0)

        assert result is True
        mock_sleep.assert_called_once_with(3.0)
        assert mock_get.call_count == 2

    @patch("labctl.power.tasmota.requests.get")
    @patch("time.sleep")
    def test_power_cycle_default_delay(self, mock_sleep, mock_get):
        """Test power cycle uses 3.0s default delay."""
        mock_response_off = Mock()
        mock_response_off.json.return_value = {"POWER": "OFF"}
        mock_response_off.raise_for_status = Mock()

        mock_response_on = Mock()
        mock_response_on.json.return_value = {"POWER": "ON"}
        mock_response_on.raise_for_status = Mock()

        mock_get.side_effect = [mock_response_off, mock_response_on]

        controller = TasmotaController("192.168.1.50")
        controller.power_cycle()

        mock_sleep.assert_called_once_with(3.0)

    @patch("labctl.power.tasmota.requests.get")
    def test_power_cycle_fails_on_off_failure(self, mock_get):
        """Test power cycle fails if power_off fails."""
        mock_get.side_effect = Exception("Connection failed")

        controller = TasmotaController("192.168.1.50")
        result = controller.power_cycle()

        assert result is False


class TestKasaRetry:
    """Tests for Kasa controller retry behavior."""

    @patch("labctl.power.kasa.time.sleep")
    def test_retry_on_auth_error_with_delay(self, mock_sleep):
        """Test that Kasa retries with 2s delay on auth failure."""
        from unittest.mock import AsyncMock

        from labctl.power.kasa import KasaController

        controller = KasaController("192.168.1.100")
        call_count = [0]

        async def _failing_coro(device, target):
            call_count[0] += 1
            raise Exception("KLAP auth failed")

        with patch.object(
            controller, "_get_device", new_callable=AsyncMock
        ) as mock_dev:
            mock_device = Mock()
            mock_device.disconnect = AsyncMock()
            mock_dev.return_value = (mock_device, mock_device)

            with pytest.raises(RuntimeError, match="KLAP auth failed"):
                controller._run(_failing_coro, "power_on")

        # Should have retried once (2 total attempts)
        assert call_count[0] == 2
        # Should have slept 2s between attempts
        mock_sleep.assert_called_once_with(2)

    @patch("labctl.power.kasa.time.sleep")
    def test_retry_succeeds_on_second_attempt(self, mock_sleep):
        """Test that Kasa succeeds on retry after initial auth failure."""
        from unittest.mock import AsyncMock

        from labctl.power.kasa import KasaController

        controller = KasaController("192.168.1.100")
        call_count = [0]

        async def _flaky_coro(device, target):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Auth error")
            return True

        with patch.object(
            controller, "_get_device", new_callable=AsyncMock
        ) as mock_dev:
            mock_device = Mock()
            mock_device.disconnect = AsyncMock()
            mock_dev.return_value = (mock_device, mock_device)

            result = controller._run(_flaky_coro, "power_on")

        assert result is True
        assert call_count[0] == 2
        mock_sleep.assert_called_once_with(2)


class TestKasaCredentials:
    """Tests for Kasa credential loading behavior."""

    def test_kasa_credentials_loaded_once_per_process(self):
        """Repeated lookups should reuse cached credentials."""
        from labctl.power import kasa as kasa_module
        from labctl.power.kasa import KasaController

        kasa_module._get_cached_kasa_credentials.cache_clear()

        mock_config = Mock()
        mock_config.kasa.username = "user@example.com"
        mock_config.kasa.password = "secret"

        fake_kasa = types.SimpleNamespace(
            Credentials=lambda username, password: (username, password)
        )

        with patch.dict(sys.modules, {"kasa": fake_kasa}):
            with patch("labctl.power.kasa.load_config", return_value=mock_config) as mock_load:
                c1 = KasaController("192.168.1.100")
                c2 = KasaController("192.168.1.101")

                assert c1._load_credentials() == ("user@example.com", "secret")
                assert c2._load_credentials() == ("user@example.com", "secret")

        assert mock_load.call_count == 1
