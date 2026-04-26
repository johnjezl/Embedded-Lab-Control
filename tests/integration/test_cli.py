"""Integration tests for labctl CLI."""

import errno
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from labctl.core import audit
from labctl.core.models import PortType
from labctl.cli import main
from labctl.core.models import Status


@pytest.fixture
def runner():
    """Create a CLI test runner."""
    return CliRunner()


class TestMainCommand:
    """Tests for the main labctl command."""

    def test_version(self, runner):
        """Test --version flag."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "labctl" in result.output
        assert "0.1.0" in result.output

    def test_help(self, runner):
        """Test --help flag."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Lab Controller" in result.output
        assert "ports" in result.output
        assert "connect" in result.output


class TestDelayOption:
    """Tests for the global --delay option."""

    def test_help_shows_delay(self, runner):
        """Test that --delay appears in help output."""
        result = runner.invoke(main, ["--help"])
        assert "--delay" in result.output
        assert "-d" in result.output

    def test_delay_zero_no_message(self, runner):
        """Test that --delay 0 produces no waiting message."""
        result = runner.invoke(main, ["--delay", "0", "ports"])
        assert result.exit_code == 0
        assert "Waiting" not in result.output

    def test_delay_shows_message(self, runner):
        """Test that --delay shows waiting message."""
        result = runner.invoke(main, ["--delay", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting 0.01s" in result.output

    def test_delay_short_flag(self, runner):
        """Test -d short flag works."""
        result = runner.invoke(main, ["-d", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting 0.01s" in result.output

    def test_delay_quiet_suppresses_message(self, runner):
        """Test that --quiet suppresses the delay message."""
        result = runner.invoke(main, ["-q", "--delay", "0.01", "ports"])
        assert result.exit_code == 0
        assert "Waiting" not in result.output

    def test_delay_actually_delays(self, runner):
        """Test that delay actually waits before executing."""
        import time

        start = time.monotonic()
        result = runner.invoke(main, ["--delay", "0.2", "ports"])
        elapsed = time.monotonic() - start

        assert result.exit_code == 0
        assert elapsed >= 0.2

    def test_delay_with_version(self, runner):
        """Test that --delay works alongside --version."""
        result = runner.invoke(main, ["--delay", "0.01", "--version"])
        assert result.exit_code == 0
        assert "labctl" in result.output

    def test_delay_invalid_value(self, runner):
        """Test that invalid delay value shows error."""
        result = runner.invoke(main, ["--delay", "abc", "ports"])
        assert result.exit_code != 0


class TestStatusCommand:
    """Tests for the status command."""

    @pytest.fixture(autouse=True)
    def clear_status_power_cache(self):
        from labctl import cli

        cli._status_power_cache.clear()
        yield
        cli._status_power_cache.clear()

    def _make_sbc(self, name: str, plug=None):
        sbc = MagicMock()
        sbc.name = name
        sbc.project = "proj"
        sbc.status = Status.ONLINE
        sbc.primary_ip = "192.168.1.10"
        sbc.power_plug = plug
        return sbc

    def test_status_fetches_power_in_parallel(self, runner):
        from labctl.power.base import PowerState

        class SlowController:
            def __init__(self, state):
                self._state = state

            def get_state(self):
                time.sleep(0.2)
                return self._state

        plugs = [
            SimpleNamespace(plug_type="tasmota", address=f"plug-{i}", plug_index=1)
            for i in range(3)
        ]
        sbcs = [
            self._make_sbc(f"sbc-{i}", plug) for i, plug in enumerate(plugs, start=1)
        ]
        manager = MagicMock()
        manager.list_sbcs.return_value = sbcs
        manager.list_active_claims.return_value = []

        states = {
            "plug-0": PowerState.ON,
            "plug-1": PowerState.OFF,
            "plug-2": PowerState.UNKNOWN,
        }

        def make_controller(plug):
            return SlowController(states[plug.address])

        start = time.monotonic()
        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.cli.PowerController.from_plug",
                side_effect=make_controller,
            ):
                result = runner.invoke(main, ["status"])
        elapsed = time.monotonic() - start

        assert result.exit_code == 0, result.output
        assert elapsed < 0.5

    def test_status_reuses_recent_power_cache(self, runner):
        from labctl.power.base import PowerState

        plug = SimpleNamespace(plug_type="tasmota", address="plug-1", plug_index=1)
        manager = MagicMock()
        manager.list_sbcs.return_value = [self._make_sbc("sbc-1", plug)]
        manager.list_active_claims.return_value = []
        mock_controller = MagicMock()
        mock_controller.get_state.return_value = PowerState.ON

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.cli.PowerController.from_plug",
                return_value=mock_controller,
            ) as mock_factory:
                with patch(
                    "labctl.cli.time.monotonic",
                    side_effect=[100.0, 100.1, 100.5],
                ):
                    first = runner.invoke(main, ["status"])
                    second = runner.invoke(main, ["status"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert mock_factory.call_count == 1


class TestPortsCommand:
    """Tests for the ports command."""

    def test_ports_runs(self, runner):
        """Test that ports command runs without error."""
        result = runner.invoke(main, ["ports"])
        # Should succeed even if no ports configured
        assert result.exit_code == 0

    def test_ports_verbose(self, runner):
        """Test ports command with verbose flag."""
        result = runner.invoke(main, ["-v", "ports"])
        assert result.exit_code == 0

    def test_ports_help(self, runner):
        """Test ports command help."""
        result = runner.invoke(main, ["ports", "--help"])
        assert result.exit_code == 0
        assert "List available serial ports" in result.output


class TestConnectCommand:
    """Tests for the connect command."""

    def test_connect_help(self, runner):
        """Test connect command help."""
        result = runner.invoke(main, ["connect", "--help"])
        assert result.exit_code == 0
        assert "Connect to a serial port console" in result.output

    def test_connect_missing_port(self, runner):
        """Test connect with missing port argument."""
        result = runner.invoke(main, ["connect"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output

    def test_connect_nonexistent_port(self, runner):
        """Test connect to non-existent port."""
        result = runner.invoke(main, ["connect", "nonexistent-port"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestSdwireReadSafety:
    """Safety checks for SDWire read commands."""

    def test_sdwire_cat_rejects_powered_on(self, runner):
        from labctl.power.base import PowerState

        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = MagicMock()
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_power = MagicMock()
        mock_power.get_state.return_value = PowerState.ON
        mock_ctrl = MagicMock()

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.power.base.PowerController.from_plug",
                return_value=mock_power,
            ):
                with patch("labctl.sdwire.controller.SDWireController", return_value=mock_ctrl):
                    result = runner.invoke(
                        main,
                        ["sdwire", "cat", "test-sbc-1", "-p", "1", "--path", "/test.txt"],
                    )

        assert result.exit_code != 0
        assert "powered on" in result.output
        assert "--force" not in result.output
        mock_ctrl.switch_to_host.assert_not_called()

    def test_sdwire_info_rejects_powered_on(self, runner):
        from labctl.power.base import PowerState

        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = MagicMock()
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_power = MagicMock()
        mock_power.get_state.return_value = PowerState.ON
        mock_ctrl = MagicMock()

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.power.base.PowerController.from_plug",
                return_value=mock_power,
            ):
                with patch("labctl.sdwire.controller.SDWireController", return_value=mock_ctrl):
                    result = runner.invoke(main, ["sdwire", "info", "test-sbc-1"])

        assert result.exit_code != 0
        assert "powered on" in result.output
        assert "--force" not in result.output
        mock_ctrl.switch_to_host.assert_not_called()

    def test_sdwire_cat_waits_for_host_settle(self, runner):
        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = None
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_ctrl = MagicMock()
        mock_ctrl.read_file.return_value = {
            "content": "x",
            "encoding": "text",
            "size": 1,
            "mtime": "2026-04-23T00:00:00Z",
            "mode": "0644",
            "truncated": False,
        }

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch("labctl.sdwire.controller.SDWireController", return_value=mock_ctrl):
                with patch("time.sleep") as mock_sleep:
                    result = runner.invoke(
                        main,
                        ["sdwire", "cat", "test-sbc-1", "-p", "1", "--path", "/test.txt"],
                    )

        assert result.exit_code == 0, result.output
        mock_sleep.assert_called_once_with(2)

    def test_sdwire_cat_reports_cleanup_failure(self, runner):
        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = None
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_ctrl = MagicMock()
        mock_ctrl.read_file.return_value = {
            "content": "x",
            "encoding": "text",
            "size": 1,
            "mtime": "2026-04-23T00:00:00Z",
            "mode": "0644",
            "truncated": False,
        }
        mock_ctrl.switch_to_dut.side_effect = RuntimeError("switch back failed")

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch("labctl.sdwire.controller.SDWireController", return_value=mock_ctrl):
                with patch("time.sleep"):
                    result = runner.invoke(
                        main,
                        ["sdwire", "cat", "test-sbc-1", "-p", "1", "--path", "/test.txt"],
                    )

        assert result.exit_code != 0
        assert "Failed to restore SD card to DUT mode" in result.output

    def test_sdwire_info_reports_cleanup_failure(self, runner):
        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = None
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_ctrl = MagicMock()
        mock_ctrl.get_disk_info.return_value = {
            "device_total_bytes": 1024,
            "disklabel_type": "msdos",
            "partitions": [],
            "free_space_regions": [],
        }
        mock_ctrl.switch_to_dut.side_effect = RuntimeError("switch back failed")

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch("labctl.sdwire.controller.SDWireController", return_value=mock_ctrl):
                with patch("time.sleep"):
                    result = runner.invoke(main, ["sdwire", "info", "test-sbc-1"])

        assert result.exit_code != 0
        assert "Failed to restore SD card to DUT mode" in result.output


class TestSdwireWritePowerFlow:
    """Write commands should still attempt power-off before host switching."""

    def test_sdwire_flash_still_powers_off_before_switch(self, runner, tmp_path):
        image = tmp_path / "image.img"
        image.write_bytes(b"img")

        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = MagicMock()
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_power = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"
        mock_ctrl.flash_image.return_value = {
            "bytes_written": 3,
            "elapsed_seconds": 0.1,
        }

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.power.base.PowerController.from_plug",
                return_value=mock_power,
            ):
                with patch(
                    "labctl.sdwire.controller.SDWireController", return_value=mock_ctrl
                ):
                    result = runner.invoke(
                        main,
                        ["sdwire", "flash", "test-sbc-1", str(image), "--no-reboot"],
                    )

        assert result.exit_code == 0, result.output
        mock_power.power_off.assert_called_once()
        mock_ctrl.switch_to_host.assert_called_once()

    def test_sdwire_update_still_powers_off_before_switch(self, runner, tmp_path):
        source = tmp_path / "kernel.img"
        source.write_bytes(b"img")

        sbc = MagicMock()
        sbc.name = "test-sbc-1"
        sbc.sdwire.serial_number = "bdgrd_sdwirec_001"
        sbc.sdwire.device_type = "sdwirec"
        sbc.power_plug = MagicMock()
        manager = MagicMock()
        manager.get_sbc_by_name.return_value = sbc
        mock_power = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.update_files.return_value = {
            "copied": ["kernel.img"],
            "renamed": [],
            "deleted": [],
        }

        with patch("labctl.cli._get_manager", return_value=manager):
            with patch(
                "labctl.power.base.PowerController.from_plug",
                return_value=mock_power,
            ):
                with patch(
                    "labctl.sdwire.controller.SDWireController", return_value=mock_ctrl
                ):
                    result = runner.invoke(
                        main,
                        [
                            "sdwire",
                            "update",
                            "test-sbc-1",
                            "-p",
                            "1",
                            "--copy",
                            f"{source}:kernel.img",
                        ],
                    )

        assert result.exit_code == 0, result.output
        mock_power.power_off.assert_called_once()
        mock_ctrl.switch_to_host.assert_called_once()
class TestProxyCommands:
    """Tests for proxy-related CLI help and guidance text."""

    @staticmethod
    def _proxy_config(tmp_path):
        db_path = tmp_path / "labctl.db"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"database_path: {db_path}\n"
            "proxy:\n"
            "  port_base: 5500\n"
        )
        return db_path, config_path

    @staticmethod
    def _seed_proxy_sbc(
        db_path,
        device_path="/dev/lab/proxy-sbc",
        *,
        add_debug_port: bool = False,
    ):
        from labctl.core.manager import get_manager

        manager = get_manager(db_path)
        manager.create_sbc(name="proxy-sbc", project="test")
        sbc = manager.get_sbc_by_name("proxy-sbc")
        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path=str(device_path),
            tcp_port=4004,
            baud_rate=115200,
            alias="proxy-console",
        )
        if add_debug_port:
            manager.assign_serial_port(
                sbc_id=sbc.id,
                port_type=PortType.DEBUG,
                device_path=str(Path(device_path).with_name("proxy-debug")),
                tcp_port=4014,
                baud_rate=115200,
                alias="proxy-debug",
            )
        return manager

    def test_proxy_group_help(self, runner):
        result = runner.invoke(main, ["proxy", "--help"])
        assert result.exit_code == 0
        assert "Share one SBC's serial console with multiple viewers" in result.output
        assert "start" in result.output
        assert "list" in result.output

    def test_proxy_start_help(self, runner):
        result = runner.invoke(main, ["proxy", "start", "--help"])
        assert result.exit_code == 0
        assert "serial output" in result.output
        assert "direct console port" in result.output
        assert "write access" in result.output
        assert "--allow-write" in result.output
        assert "--exit-on-disconnect" in result.output
        assert "--reconnect" in result.output

    def test_proxy_list_explains_foreground_workflow(self, runner):
        result = runner.invoke(main, ["proxy", "list"])
        assert result.exit_code == 0
        assert "Shared serial-console path" in result.output
        assert "proxy start <sbc>" in result.output

    def test_sessions_explains_proxy_scope(self, runner):
        result = runner.invoke(main, ["sessions"])
        assert result.exit_code == 0
        assert "Shared serial proxy only" in result.output
        assert "serial_capture" in result.output
        assert "proxy start <sbc>" in result.output

    def test_proxy_start_reports_port_in_use_cleanly(self, tmp_path, monkeypatch):
        db_path, config_path = self._proxy_config(tmp_path)
        self._seed_proxy_sbc(db_path)

        class FakeProxy:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                raise OSError(errno.EADDRINUSE, "address already in use")

            async def stop(self):
                return None

        monkeypatch.setattr("labctl.serial.proxy.SerialProxy", FakeProxy)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", str(config_path), "proxy", "start", "proxy-sbc", "--port", "5500"],
        )

        assert result.exit_code != 0
        assert "already in use" in result.output
        assert "--port" in result.output

    def test_proxy_start_is_read_only_by_default(self, runner, tmp_path, monkeypatch):
        db_path, config_path = self._proxy_config(tmp_path)
        self._seed_proxy_sbc(db_path)

        class FakeProxy:
            def __init__(self, *args, **kwargs):
                self.is_running = False

            async def start(self):
                return None

            async def stop(self):
                return None

        monkeypatch.setattr("labctl.serial.proxy.SerialProxy", FakeProxy)

        result = runner.invoke(
            main, ["-c", str(config_path), "proxy", "start", "proxy-sbc", "--port", "5500"]
        )

        assert result.exit_code == 0
        assert "write access: disabled (read-only)" in result.output
        assert "This proxy is read-only" in result.output

    def test_proxy_start_allow_write_changes_guidance(self, runner, tmp_path, monkeypatch):
        db_path, config_path = self._proxy_config(tmp_path)
        self._seed_proxy_sbc(db_path)

        class FakeProxy:
            def __init__(self, *args, **kwargs):
                self.is_running = False

            async def start(self):
                return None

            async def stop(self):
                return None

        monkeypatch.setattr("labctl.serial.proxy.SerialProxy", FakeProxy)

        result = runner.invoke(
            main,
            [
                "-c",
                str(config_path),
                "proxy",
                "start",
                "proxy-sbc",
                "--port",
                "5500",
                "--allow-write",
            ],
        )

        assert result.exit_code == 0
        assert "write access: enabled" in result.output
        assert "write policy: first" in result.output
        assert "The first client to type" in result.output

    def test_connect_refuses_when_proxy_is_active(self, runner, tmp_path):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        result = runner.invoke(main, ["-c", str(config_path), "connect", "proxy-console"])

        assert result.exit_code != 0
        assert "Shared proxy is active" in result.output
        assert "Refusing direct 'connect' access" in result.output
        assert "nc localhost 5500" in result.output

    def test_connect_refuses_when_proxy_is_active_for_sbc_name(self, runner, tmp_path):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        result = runner.invoke(main, ["-c", str(config_path), "connect", "proxy-sbc"])

        assert result.exit_code != 0
        assert "Refusing direct 'connect' access" in result.output
        assert "nc localhost 5500" in result.output

    def test_connect_refuses_when_proxy_is_active_for_device_path(self, runner, tmp_path):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        device_path = tmp_path / "proxy-sbc"
        real_device_path = tmp_path / "ttyUSB0"
        real_device_path.write_text("")
        device_path.symlink_to(real_device_path)
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path, device_path=device_path)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        result = runner.invoke(
            main, ["-c", str(config_path), "connect", str(real_device_path)]
        )

        assert result.exit_code != 0
        assert "Refusing direct 'connect' access" in result.output
        assert "nc localhost 5500" in result.output

    def test_connect_allows_non_console_alias_when_proxy_is_active(
        self, runner, tmp_path, monkeypatch
    ):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path, add_debug_port=True)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        calls = []

        def fake_connect_tcp(host, port):
            calls.append((host, port))

        monkeypatch.setattr("labctl.cli._connect_tcp", fake_connect_tcp)

        result = runner.invoke(main, ["-c", str(config_path), "connect", "proxy-debug"])

        assert result.exit_code == 0
        assert calls == [("localhost", 4014)]
        assert "Refusing direct 'connect' access" not in result.output

    def test_console_refuses_when_proxy_is_active(self, runner, tmp_path):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        result = runner.invoke(main, ["-c", str(config_path), "console", "proxy-sbc"])

        assert result.exit_code != 0
        assert "Shared proxy is active" in result.output
        assert "Refusing direct 'console' access" in result.output
        assert "telnet localhost 5500" in result.output

    def test_console_refuses_when_proxy_is_active_and_ser2net_disabled(
        self, runner, tmp_path
    ):
        from labctl.serial.proxy import write_proxy_state

        db_path, _ = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"database_path: {db_path}\n"
            "ser2net:\n"
            "  enabled: false\n"
            "proxy:\n"
            f"  log_dir: {log_dir}\n"
        )

        self._seed_proxy_sbc(db_path)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        result = runner.invoke(main, ["-c", str(config_path), "console", "proxy-sbc"])

        assert result.exit_code != 0
        assert "Shared proxy is active" in result.output
        assert "Refusing direct 'console' access" in result.output
        assert "telnet localhost 5500" in result.output

    def test_console_allows_non_console_type_when_proxy_is_active(
        self, runner, tmp_path, monkeypatch
    ):
        from labctl.serial.proxy import write_proxy_state

        db_path, config_path = self._proxy_config(tmp_path)
        log_dir = tmp_path / "logs"
        config_path.write_text(f"database_path: {db_path}\nproxy:\n  log_dir: {log_dir}\n")

        self._seed_proxy_sbc(db_path, add_debug_port=True)
        write_proxy_state(
            name="proxy-sbc",
            log_dir=log_dir,
            proxy_port=5500,
            ser2net_port=4004,
            allow_write=False,
        )

        calls = []

        def fake_connect_tcp(host, port):
            calls.append((host, port))

        monkeypatch.setattr("labctl.cli._connect_tcp", fake_connect_tcp)

        result = runner.invoke(
            main, ["-c", str(config_path), "console", "proxy-sbc", "--type", "debug"]
        )

        assert result.exit_code == 0
        assert calls == [("localhost", 4014)]
        assert "Refusing direct 'console' access" not in result.output


@pytest.fixture
def claim_runner(tmp_path, monkeypatch):
    """CLI runner with a throwaway DB so claim commands write nowhere real."""
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"database_path: {db_path}\n"
        "claims:\n"
        "  enabled: true\n"
        "  default_duration_minutes: 30\n"
        "  max_duration_minutes: 60\n"
        "  min_duration_minutes: 1\n"
        "  grace_period_seconds: 0\n"
    )

    # Every CLI invocation creates a fresh manager from config — we
    # seed the DB via a manager instance up front.
    from labctl.core.manager import get_manager

    manager = get_manager(db_path)
    manager.create_sbc(name="pi-5-1", project="test")
    manager.create_sbc(name="pi-5-2", project="test")

    runner = CliRunner()
    return runner, config_path, manager


class TestClaimCommands:
    """Integration tests for claim/release/renew/claims CLI commands."""

    def test_claim_and_release_round_trip(self, claim_runner):
        runner, config, manager = claim_runner

        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "10m", "-r", "bringup"],
        )
        assert result.exit_code == 0, result.output
        assert "Claimed 'pi-5-1'" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "list"])
        assert result.exit_code == 0
        assert "pi-5-1" in result.output

        result = runner.invoke(main, ["-c", str(config), "release", "pi-5-1"])
        assert result.exit_code == 0

        result = runner.invoke(main, ["-c", str(config), "claims", "list"])
        assert result.exit_code == 0
        assert "No active claims" in result.output

    def test_renew_show_history_and_stats(self, claim_runner):
        runner, config, manager = claim_runner

        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "10m", "-r", "bringup"],
        )
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            main,
            ["-c", str(config), "renew", "pi-5-1", "-d", "20m"],
        )
        assert result.exit_code == 0, result.output
        assert "Renewed claim on 'pi-5-1'" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "show", "pi-5-1"])
        assert result.exit_code == 0, result.output
        assert "held by" in result.output
        assert "reason: bringup" in result.output
        assert "renewed 1x" in result.output

        result = runner.invoke(main, ["-c", str(config), "release", "pi-5-1"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            main,
            ["-c", str(config), "claims", "history", "pi-5-1"],
        )
        assert result.exit_code == 0, result.output
        assert "Last 1 claim(s) on 'pi-5-1':" in result.output
        assert "[released]" in result.output
        assert "reason: bringup" in result.output

        result = runner.invoke(main, ["-c", str(config), "claims", "stats"])
        assert result.exit_code == 0, result.output
        assert "Total claims:" in result.output
        assert "Released:" in result.output

    def test_claim_duration_out_of_bounds(self, claim_runner):
        runner, config, _ = claim_runner
        # Config max is 60 minutes; 4h should be rejected.
        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-d", "4h", "-r", "too long"],
        )
        assert result.exit_code != 0
        assert "out of bounds" in result.output.lower()

    def test_claim_conflict_exit_nonzero(self, claim_runner):
        runner, config, manager = claim_runner
        # First claim via manager (different session id)
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "claim", "pi-5-1", "-r", "mine"],
        )
        assert result.exit_code != 0
        assert "already claimed" in result.output

    def test_force_release_overrides(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            [
                "-c",
                str(config),
                "force-release",
                "pi-5-1",
                "-r",
                "operator takeover",
            ],
        )
        assert result.exit_code == 0
        assert manager.get_active_claim("pi-5-1") is None

    def test_request_release_records_request(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other",
            session_id="cli-someone-else@host",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = runner.invoke(
            main,
            [
                "-c",
                str(config),
                "request-release",
                "pi-5-1",
                "-r",
                "need the bench",
            ],
        )
        assert result.exit_code == 0
        claim = manager.get_active_claim("pi-5-1")
        assert len(claim.pending_requests) == 1

        result = runner.invoke(main, ["-c", str(config), "claims", "show", "pi-5-1"])
        assert result.exit_code == 0, result.output
        assert "request from" in result.output
        assert "need the bench" in result.output

    def test_status_surfaces_claim(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="other-agent",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="bringup",
        )
        result = runner.invoke(main, ["-c", str(config), "status"])
        assert result.exit_code == 0
        assert "other-agent" in result.output
        assert "CLAIM" in result.output

    def test_remove_blocked_by_claim_without_force(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="holder",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="r",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "remove", "pi-5-1", "--yes"],
        )
        assert result.exit_code != 0
        assert "claimed" in result.output.lower()
        assert manager.get_sbc_by_name("pi-5-1") is not None

    def test_remove_force_succeeds_while_claimed(self, claim_runner):
        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-2",
            agent_name="holder",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=600,
            reason="r",
        )
        result = runner.invoke(
            main,
            ["-c", str(config), "remove", "pi-5-2", "--yes", "--force"],
        )
        assert result.exit_code == 0
        assert manager.get_sbc_by_name("pi-5-2") is None

    def test_claims_expire_sweeps(self, claim_runner):
        """labctl claims expire releases past-deadline claims."""
        import time

        runner, config, manager = claim_runner
        manager.claim_sbc(
            sbc_name="pi-5-1",
            agent_name="short",
            session_id="cli-x@h",
            session_kind="cli",
            duration_seconds=1,
            reason="short",
        )
        time.sleep(1.2)
        result = runner.invoke(
            main,
            ["-c", str(config), "claims", "expire"],
        )
        assert result.exit_code == 0
        assert "expired" in result.output.lower() or "Released" in result.output
        assert manager.get_active_claim("pi-5-1") is None


@pytest.fixture
def activity_runner(tmp_path):
    """CLI runner with a throwaway DB for activity-tail tests."""
    db_path = tmp_path / "labctl.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database_path: {db_path}\n")

    from labctl.core.manager import get_manager

    manager = get_manager(db_path)
    runner = CliRunner()
    return runner, config_path, manager


class TestActivityCommands:
    """Integration tests for activity-stream CLI commands."""

    def test_activity_tail_lists_events(self, activity_runner):
        runner, config, manager = activity_runner
        with audit.activity_context("cli:alice", "cli"):
            manager.create_sbc(name="activity-pi", project="test")

        result = runner.invoke(main, ["-c", str(config), "activity", "tail"])

        assert result.exit_code == 0
        assert "create" in result.output
        assert "activity-pi" in result.output
        assert "cli:alice" in result.output

    def test_activity_tail_filters_by_actor(self, activity_runner):
        runner, config, manager = activity_runner
        with audit.activity_context("cli:alice", "cli"):
            manager.create_sbc(name="alice-pi", project="test")
        with audit.activity_context("cli:bob", "cli"):
            manager.create_sbc(name="bob-pi", project="test")

        result = runner.invoke(
            main,
            ["-c", str(config), "activity", "tail", "--actor", "cli:alice"],
        )

        assert result.exit_code == 0
        assert "alice-pi" in result.output
        assert "bob-pi" not in result.output

    def test_activity_export_ndjson(self, activity_runner):
        runner, config, manager = activity_runner
        with audit.activity_context("cli:alice", "cli"):
            manager.create_sbc(name="export-pi", project="test")

        result = runner.invoke(
            main,
            ["-c", str(config), "activity", "export", "--format", "ndjson"],
        )

        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines
        payload = __import__("json").loads(lines[-1])
        assert payload["entity_name"] == "export-pi"
        assert payload["actor"] == "cli:alice"


# ---------------------------------------------------------------------------
# `labctl connect` raw-mode TTY / Ctrl+] escape state machine
# ---------------------------------------------------------------------------


class TestConnectEscapeStateMachine:
    """Pure-function tests for the Ctrl+] escape parser used by raw-mode
    `labctl connect`. No sockets, no terminals — just byte handling."""

    def setup_method(self):
        from labctl.cli import _process_keystrokes

        self.process = _process_keystrokes

    def test_plain_input_passes_through(self):
        out, escape, exit_ = self.process(b"hello", False)
        assert out == b"hello"
        assert escape is False
        assert exit_ is False

    def test_single_escape_sets_pending_state(self):
        """A trailing Ctrl+] in a chunk must set in_escape for the next read."""
        out, escape, exit_ = self.process(b"\x1d", False)
        assert out == b""
        assert escape is True
        assert exit_ is False

    def test_escape_then_q_exits(self):
        """Ctrl+] q in one chunk → exit."""
        out, escape, exit_ = self.process(b"\x1dq", False)
        assert exit_ is True

    def test_escape_then_q_split_across_chunks(self):
        """Common boot-menu typing pattern: keys arrive byte-by-byte."""
        out1, escape1, exit1 = self.process(b"\x1d", False)
        assert escape1 is True and exit1 is False
        out2, escape2, exit2 = self.process(b"q", escape1)
        assert exit2 is True
        assert escape2 is False

    def test_escape_then_ctrl_backslash_also_exits(self):
        out, escape, exit_ = self.process(b"\x1d\x1c", False)
        assert exit_ is True

    def test_doubled_escape_sends_one_literal(self):
        """Ctrl+] Ctrl+] → forward one literal Ctrl+] to the remote."""
        out, escape, exit_ = self.process(b"\x1d\x1d", False)
        assert out == b"\x1d"
        assert escape is False
        assert exit_ is False

    def test_escape_then_other_byte_forwards_both(self):
        """Ctrl+] X (X != q/Q/Ctrl+\\/Ctrl+]) → forward Ctrl+] then X."""
        out, escape, exit_ = self.process(b"\x1da", False)
        assert out == b"\x1da"
        assert escape is False
        assert exit_ is False

    def test_text_before_and_after_escape_in_one_chunk(self):
        """Ensure the parser reads bytes left-to-right within a chunk."""
        out, escape, exit_ = self.process(b"hi\x1dabye", False)
        assert out == b"hi\x1dabye"
        assert escape is False
        assert exit_ is False

    def test_typing_before_escape_then_q_in_next_chunk(self):
        """Pre-escape text is forwarded; q in next chunk still exits."""
        out1, escape1, exit1 = self.process(b"foo\x1d", False)
        assert out1 == b"foo"
        assert escape1 is True and exit1 is False
        out2, escape2, exit2 = self.process(b"q", escape1)
        assert out2 == b""
        assert exit2 is True


class TestConnectTcpDispatch:
    """`_connect_tcp` picks raw mode for TTYs, subprocess fallback otherwise."""

    def test_non_tty_stdin_falls_back_to_subprocess(self, monkeypatch):
        from labctl import cli

        calls = []
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: calls.append(("run", a, k))
        )

        # Sentinel that fails the test if the raw path is taken.
        def boom(*a, **k):
            raise AssertionError("Should not enter raw mode for non-TTY stdin")

        monkeypatch.setattr(cli, "_connect_tcp_raw", boom)
        cli._connect_tcp("localhost", 4007)

        assert calls, "subprocess fallback should have been invoked"
        # First positional arg is the argv list.
        argv = calls[0][1][0]
        assert argv == ["nc", "localhost", "4007"]

    def test_non_tty_stdout_also_falls_back(self, monkeypatch):
        """If stdout is redirected to a file, raw mode is unsafe."""
        from labctl import cli

        calls = []
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: calls.append(("run", a, k))
        )
        monkeypatch.setattr(
            cli, "_connect_tcp_raw",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("Should not enter raw mode")
            ),
        )

        cli._connect_tcp("localhost", 4007)
        assert calls

    def test_tty_stdio_uses_raw_mode(self, monkeypatch):
        from labctl import cli

        called = []
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(
            cli, "_connect_tcp_raw", lambda h, p: called.append((h, p))
        )
        # Should NOT be invoked.
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("Should not call subprocess from TTY path")
            ),
        )

        cli._connect_tcp("localhost", 4007)
        assert called == [("localhost", 4007)]
