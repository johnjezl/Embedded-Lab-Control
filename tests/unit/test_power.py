"""Unit tests for power control module."""

import logging
import sys
import types
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
        """Test that Kasa retries twice with 2s delay on auth failure."""
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

        # Default retries=2 → 3 total attempts, 2 sleeps between them.
        assert call_count[0] == 3
        assert mock_sleep.call_count == 2
        for call in mock_sleep.call_args_list:
            assert call.args == (2,)

    @patch("labctl.power.kasa.time.sleep")
    def test_retry_logs_warning_and_final_error(self, mock_sleep, caplog):
        """Retry attempts log at WARNING; exhausted retries log at ERROR."""
        import logging
        from unittest.mock import AsyncMock

        from labctl.power.kasa import KasaController

        controller = KasaController("192.168.1.100")

        async def _failing_coro(device, target):
            raise Exception("KLAP auth failed")

        with patch.object(
            controller, "_get_device", new_callable=AsyncMock
        ) as mock_dev:
            mock_device = Mock()
            mock_device.disconnect = AsyncMock()
            mock_dev.return_value = (mock_device, mock_device)

            with caplog.at_level(logging.WARNING, logger="labctl.power.kasa"):
                with pytest.raises(RuntimeError):
                    controller._run(_failing_coro, "power_on")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        # 2 retry attempts → 2 warnings; final failure → 1 error.
        assert len(warnings) == 2
        assert len(errors) == 1
        assert "retrying" in warnings[0].getMessage()
        assert "after 3 attempts" in errors[0].getMessage()

    @patch("labctl.power.kasa.time.sleep")
    def test_retry_success_logs_info(self, mock_sleep, caplog):
        """A retry that ultimately succeeds logs an INFO recovery line."""
        import logging
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

            with caplog.at_level(logging.INFO, logger="labctl.power.kasa"):
                result = controller._run(_flaky_coro, "power_on")

        assert result is True
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "succeeded on attempt" in r.getMessage()
        ]
        assert len(infos) == 1

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


class TestKasaKlapV2Hint:
    """A persistent AuthenticationError names the python-kasa < 0.12 cause."""

    @staticmethod
    def _fail_with(exc_cls, kasa_version, credentials=True):
        from unittest.mock import AsyncMock

        from labctl.power.kasa import KasaController

        controller = KasaController("192.168.1.100", plug_index=2)

        async def _failing_coro(device, target):
            raise exc_cls("Device response did not match our challenge")

        with (
            patch.object(controller, "_get_device", new_callable=AsyncMock) as dev,
            patch("labctl.power.kasa.time.sleep"),
            patch("importlib.metadata.version", return_value=kasa_version),
            patch.object(
                controller,
                "_load_credentials",
                return_value=Mock() if credentials else None,
            ),
        ):
            mock_device = Mock()
            mock_device.disconnect = AsyncMock()
            dev.return_value = (mock_device, mock_device)
            with pytest.raises(RuntimeError) as excinfo:
                controller._run(_failing_coro, "power_on")
        return str(excinfo.value)

    def test_auth_error_on_old_python_kasa_names_the_upgrade(self):
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        msg = self._fail_with(AuthenticationError, "0.10.2")
        assert "after 3 attempts" in msg
        assert "python-kasa 0.10.2 can't authenticate" in msg
        assert "python-kasa >= 0.12" in msg

    def test_no_hint_on_python_kasa_0_12(self):
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        msg = self._fail_with(AuthenticationError, "0.12.0")
        assert "can't authenticate" not in msg

    def test_no_hint_without_credentials(self):
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        msg = self._fail_with(AuthenticationError, "0.10.2", credentials=False)
        assert "after 3 attempts" in msg
        assert "can't authenticate" not in msg

    def test_no_hint_for_other_errors(self):
        msg = self._fail_with(TimeoutError, "0.10.2")
        assert "can't authenticate" not in msg

    def test_no_hint_when_version_unknown(self):
        from importlib.metadata import PackageNotFoundError

        from labctl.power.kasa import _klap_v2_hint

        with patch("importlib.metadata.version", side_effect=PackageNotFoundError("x")):
            assert _klap_v2_hint() == ""
        with patch("importlib.metadata.version", return_value="1.0b1"):
            assert _klap_v2_hint() == ""


class TestKasaStripDiscoveredAsPlug:
    """KLAP discovery types an HS300 as IotPlug; its outlets must survive."""

    @staticmethod
    def _discovered(sys_info):
        from unittest.mock import AsyncMock

        from kasa.iot import IotPlug

        device = Mock(spec=IotPlug)
        device.host = "192.168.1.100"
        device.alias = "strip"
        device.protocol = Mock(name="protocol")
        device.sys_info = sys_info
        device.children = []
        device.update = AsyncMock()
        device.disconnect = AsyncMock()
        return device

    @staticmethod
    def _discover(device, strip_update=None):
        import asyncio
        from unittest.mock import AsyncMock

        from kasa.iot import IotStrip

        from labctl.power.kasa import KasaController

        controller = KasaController("192.168.1.100", plug_index=2)
        with (
            patch(
                "kasa.Discover.discover_single",
                new_callable=AsyncMock,
                return_value=device,
            ) as discover,
            patch.object(controller, "_load_credentials", return_value=None),
            patch.object(
                IotStrip, "update", new_callable=AsyncMock, side_effect=strip_update
            ) as update,
        ):
            result = asyncio.run(controller._discover_and_update())
        return result, discover, update

    def test_plug_with_children_in_sysinfo_becomes_strip(self):
        from kasa.iot import IotStrip

        device = self._discovered({"children": [{"id": "00"}, {"id": "01"}]})
        result, discover, update = self._discover(device)

        assert isinstance(result, IotStrip)
        # Same KLAP session: the strip reuses the discovered protocol.
        assert result.protocol is device.protocol
        assert discover.await_count == 1
        device.update.assert_awaited_once()
        update.assert_awaited_once()
        device.disconnect.assert_not_awaited()

    def test_plug_without_children_is_left_alone(self):
        device = self._discovered({"relay_state": 1})
        result, _, update = self._discover(device)

        assert result is device
        update.assert_not_awaited()

    def test_non_iot_device_is_left_alone(self):
        from unittest.mock import AsyncMock

        device = Mock()  # e.g. a SMART-protocol device: not an IotDevice
        device.children = []
        device.update = AsyncMock()
        device.disconnect = AsyncMock()
        result, _, update = self._discover(device)

        assert result is device
        update.assert_not_awaited()

    def test_strip_update_failure_disconnects(self):
        device = self._discovered({"children": [{"id": "00"}]})
        with pytest.raises(OSError, match="boom"):
            self._discover(device, strip_update=OSError("boom"))
        device.disconnect.assert_awaited_once()


class TestKasaRunningLoopErrors:
    def test_kasa_error_survives_running_event_loop(self):
        """Inside a running loop (the MCP server) the driver's own error
        is reported, not an asyncio one."""
        import asyncio

        from labctl.power.kasa import KasaController

        controller = KasaController("10.0.0.5", plug_index=2)

        async def failing():
            raise RuntimeError("single-outlet plug")

        async def main():
            controller._run_async(failing, "power_on", retries=0)

        with pytest.raises(RuntimeError, match="single-outlet"):
            asyncio.run(main())


class TestKasaHostStateCache:
    """Per-host state cache eliminates parallel KLAP handshakes per cycle."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        from labctl.power import kasa as kasa_module

        kasa_module._clear_kasa_caches()
        yield
        kasa_module._clear_kasa_caches()

    def _patch_fetch(self, *, states_seq):
        """Return a context manager patching `_fetch_all_outlet_states`.

        ``states_seq`` is a list of dicts; each call pops one. Tests can
        inspect call_count via the returned mock.
        """
        from unittest.mock import patch as _patch

        calls = {"n": 0}

        def fake_fetch(self):
            i = calls["n"]
            calls["n"] += 1
            return states_seq[min(i, len(states_seq) - 1)]

        ctx = _patch.object(
            __import__("labctl.power.kasa", fromlist=["KasaController"]).KasaController,
            "_fetch_all_outlet_states",
            new=fake_fetch,
        )
        return ctx, calls

    def test_concurrent_callers_share_one_handshake(self):
        """6 outlets on one strip → one fetch, five cache hits."""
        import threading

        from labctl.power.kasa import KasaController

        host = "192.168.4.140"
        controllers = [KasaController(host, plug_index=i) for i in range(1, 7)]
        # The strip has 6 outlets; first fetch returns the full state map.
        ctx, calls = self._patch_fetch(
            states_seq=[{1: True, 2: False, 3: True, 4: False, 5: True, 6: False}]
        )

        results: list = [None] * len(controllers)
        threads: list[threading.Thread] = []

        def worker(i, c):
            results[i] = c.get_state()

        with ctx:
            for i, c in enumerate(controllers):
                threads.append(threading.Thread(target=worker, args=(i, c)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        from labctl.power.base import PowerState

        assert calls["n"] == 1, "expected exactly one device fetch for the strip"
        assert results == [
            PowerState.ON,
            PowerState.OFF,
            PowerState.ON,
            PowerState.OFF,
            PowerState.ON,
            PowerState.OFF,
        ]

    def test_cache_ttl_expires(self):
        from labctl.power.kasa import KasaController

        host = "10.0.0.50"
        ctx, calls = self._patch_fetch(states_seq=[{1: True}, {1: False}])

        # Sequence of monotonic calls during the three get_state()s:
        #   #1: cache empty → no TTL check → store @ T=100.0
        #   #2: TTL check @ T=100.5 (within TTL) → cache hit
        #   #3: TTL check @ T=200.0 (past TTL) → miss
        #       in-lock TTL re-check @ T=200.1 → still miss
        #       store @ T=200.2
        with ctx:
            with patch(
                "labctl.power.kasa.time.monotonic",
                side_effect=[100.0, 100.5, 200.0, 200.1, 200.2],
            ):
                c = KasaController(host)
                assert c.get_state().value == "on"
                assert c.get_state().value == "on"  # cache hit
                assert c.get_state().value == "off"  # TTL expired → refetch

        assert calls["n"] == 2

    def test_different_hosts_do_not_share_cache(self):
        from labctl.power.kasa import KasaController

        ctx, calls = self._patch_fetch(states_seq=[{1: True}, {1: False}])
        with ctx:
            assert KasaController("10.0.0.1").get_state().value == "on"
            assert KasaController("10.0.0.2").get_state().value == "off"

        assert calls["n"] == 2

    def test_power_off_invalidates_cache(self):
        """A successful write drops the cache so the next read reflects it."""
        from labctl.power import kasa as kasa_module
        from labctl.power.kasa import KasaController

        host = "10.0.0.99"
        ctx_fetch, calls = self._patch_fetch(states_seq=[{1: True}, {1: False}])

        with ctx_fetch:
            c = KasaController(host)
            assert c.get_state().value == "on"
            assert calls["n"] == 1

            # Stub _run so power_off doesn't actually network — we only care
            # about the invalidation side effect.
            with patch.object(KasaController, "_run", return_value=True):
                assert c.power_off() is True

            # Cache must be empty now → second get_state refetches.
            assert kasa_module._read_cached_state(host, 1) is None
            assert c.get_state().value == "off"
            assert calls["n"] == 2

    def test_cache_miss_returns_unknown_for_absent_channel(self):
        """If the strip enumerates fewer channels than expected, fall to UNKNOWN."""
        from labctl.power.base import PowerState
        from labctl.power.kasa import KasaController

        ctx, _ = self._patch_fetch(states_seq=[{1: True, 2: False}])
        with ctx:
            assert (
                KasaController("10.0.0.10", plug_index=99).get_state()
                == PowerState.UNKNOWN
            )


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
            with patch(
                "labctl.power.kasa.load_config", return_value=mock_config
            ) as mock_load:
                c1 = KasaController("192.168.1.100")
                c2 = KasaController("192.168.1.101")

                assert c1._load_credentials() == ("user@example.com", "secret")
                assert c2._load_credentials() == ("user@example.com", "secret")

        assert mock_load.call_count == 1
