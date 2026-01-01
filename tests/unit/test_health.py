"""Unit tests for health check module."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from labctl.core.models import Status
from labctl.health.alerts import (
    Alert,
    AlertLevel,
    AlertManager,
    LogAlertHandler,
)
from labctl.health.checks import (
    CheckResult,
    CheckType,
    HealthChecker,
    HealthCheckSummary,
)
from labctl.power.base import PowerState


class TestCheckResult:
    """Tests for CheckResult dataclass."""

    def test_create_result(self):
        """Test creating a check result."""
        result = CheckResult(
            sbc_name="test-pi",
            check_type=CheckType.PING,
            success=True,
            message="Host reachable",
            duration_ms=50.5,
        )

        assert result.sbc_name == "test-pi"
        assert result.check_type == CheckType.PING
        assert result.success is True
        assert result.message == "Host reachable"
        assert result.duration_ms == 50.5
        assert isinstance(result.timestamp, datetime)

    def test_status_char_success(self):
        """Test status character for successful check."""
        result = CheckResult(
            sbc_name="test",
            check_type=CheckType.PING,
            success=True,
            message="OK",
            duration_ms=10,
        )
        assert result.status_char == "\u2713"

    def test_status_char_failure(self):
        """Test status character for failed check."""
        result = CheckResult(
            sbc_name="test",
            check_type=CheckType.PING,
            success=False,
            message="Failed",
            duration_ms=10,
        )
        assert result.status_char == "\u2717"


class TestHealthCheckSummary:
    """Tests for HealthCheckSummary dataclass."""

    def test_determine_status_ping_fail(self):
        """Test status determination when ping fails."""
        summary = HealthCheckSummary(sbc_name="test")
        summary.ping_result = CheckResult(
            sbc_name="test",
            check_type=CheckType.PING,
            success=False,
            message="Unreachable",
            duration_ms=2000,
        )

        assert summary.determine_status() == Status.OFFLINE

    def test_determine_status_power_off(self):
        """Test status determination when power is off."""
        summary = HealthCheckSummary(sbc_name="test")
        summary.power_state = PowerState.OFF

        assert summary.determine_status() == Status.OFFLINE

    def test_determine_status_ping_ok_serial_fail(self):
        """Test status when ping ok but serial fails."""
        summary = HealthCheckSummary(sbc_name="test")
        summary.ping_result = CheckResult(
            sbc_name="test",
            check_type=CheckType.PING,
            success=True,
            message="OK",
            duration_ms=10,
        )
        summary.serial_result = CheckResult(
            sbc_name="test",
            check_type=CheckType.SERIAL,
            success=False,
            message="Connection refused",
            duration_ms=100,
        )

        assert summary.determine_status() == Status.ERROR

    def test_determine_status_online(self):
        """Test status determination when all checks pass."""
        summary = HealthCheckSummary(sbc_name="test")
        summary.ping_result = CheckResult(
            sbc_name="test",
            check_type=CheckType.PING,
            success=True,
            message="OK",
            duration_ms=10,
        )
        summary.serial_result = CheckResult(
            sbc_name="test",
            check_type=CheckType.SERIAL,
            success=True,
            message="OK",
            duration_ms=10,
        )

        assert summary.determine_status() == Status.ONLINE


class TestHealthChecker:
    """Tests for HealthChecker class."""

    def test_init(self):
        """Test health checker initialization."""
        checker = HealthChecker(ping_timeout=3.0, serial_timeout=5.0)

        assert checker.ping_timeout == 3.0
        assert checker.serial_timeout == 5.0

    @patch("subprocess.run")
    def test_ping_check_success(self, mock_run):
        """Test successful ping check."""
        mock_run.return_value = MagicMock(returncode=0)

        checker = HealthChecker()
        result = checker.ping_check("192.168.1.100", "test-pi")

        assert result.success is True
        assert result.check_type == CheckType.PING
        assert result.sbc_name == "test-pi"
        assert "reachable" in result.message

    @patch("subprocess.run")
    def test_ping_check_failure(self, mock_run):
        """Test failed ping check."""
        mock_run.return_value = MagicMock(returncode=1)

        checker = HealthChecker()
        result = checker.ping_check("192.168.1.100", "test-pi")

        assert result.success is False
        assert "unreachable" in result.message

    @patch("socket.socket")
    def test_serial_check_success(self, mock_socket_class):
        """Test successful serial port check."""
        mock_socket = MagicMock()
        mock_socket_class.return_value = mock_socket

        checker = HealthChecker()
        result = checker.serial_check("localhost", 4000, "test-pi")

        assert result.success is True
        assert result.check_type == CheckType.SERIAL
        mock_socket.connect.assert_called_once_with(("localhost", 4000))
        mock_socket.close.assert_called_once()

    @patch("socket.socket")
    def test_serial_check_connection_refused(self, mock_socket_class):
        """Test serial check with connection refused."""
        mock_socket = MagicMock()
        mock_socket.connect.side_effect = ConnectionRefusedError()
        mock_socket_class.return_value = mock_socket

        checker = HealthChecker()
        result = checker.serial_check("localhost", 4000, "test-pi")

        assert result.success is False
        assert "refused" in result.message

    def test_power_check_success(self):
        """Test successful power check."""
        mock_controller = MagicMock()
        mock_controller.get_state.return_value = PowerState.ON

        checker = HealthChecker()
        result, state = checker.power_check(mock_controller, "test-pi")

        assert result.success is True
        assert state == PowerState.ON
        assert "ON" in result.message

    def test_power_check_unknown(self):
        """Test power check with unknown state."""
        mock_controller = MagicMock()
        mock_controller.get_state.return_value = PowerState.UNKNOWN

        checker = HealthChecker()
        result, state = checker.power_check(mock_controller, "test-pi")

        assert result.success is False
        assert state == PowerState.UNKNOWN

    def test_check_sbc_with_ip(self):
        """Test checking SBC with IP address."""
        mock_sbc = MagicMock()
        mock_sbc.name = "test-pi"
        mock_sbc.primary_ip = "192.168.1.100"
        mock_sbc.serial_ports = []
        mock_sbc.power_plug = None

        with patch.object(HealthChecker, "ping_check") as mock_ping:
            mock_ping.return_value = CheckResult(
                sbc_name="test-pi",
                check_type=CheckType.PING,
                success=True,
                message="OK",
                duration_ms=10,
            )

            checker = HealthChecker()
            summary = checker.check_sbc(mock_sbc, [CheckType.PING])

            assert summary.ping_result is not None
            assert summary.ping_result.success is True
            mock_ping.assert_called_once()


class TestAlertLevel:
    """Tests for AlertLevel enum."""

    def test_alert_levels(self):
        """Test alert level values."""
        assert AlertLevel.INFO.value == "info"
        assert AlertLevel.WARNING.value == "warning"
        assert AlertLevel.CRITICAL.value == "critical"


class TestAlert:
    """Tests for Alert dataclass."""

    def test_create_alert(self):
        """Test creating an alert."""
        alert = Alert(
            level=AlertLevel.WARNING,
            sbc_name="test-pi",
            message="SBC went offline",
            details="Ping failed",
        )

        assert alert.level == AlertLevel.WARNING
        assert alert.sbc_name == "test-pi"
        assert alert.message == "SBC went offline"
        assert alert.details == "Ping failed"

    def test_alert_format(self):
        """Test alert string formatting."""
        alert = Alert(
            level=AlertLevel.CRITICAL,
            sbc_name="test-pi",
            message="Power failure",
        )

        formatted = alert.format()
        assert "CRITICAL" in formatted
        assert "test-pi" in formatted
        assert "Power failure" in formatted


class TestLogAlertHandler:
    """Tests for LogAlertHandler class."""

    def test_send_alert(self, tmp_path):
        """Test sending alert to log file."""
        log_path = tmp_path / "alerts.log"
        handler = LogAlertHandler(log_path)

        alert = Alert(
            level=AlertLevel.INFO,
            sbc_name="test-pi",
            message="Test alert",
        )

        result = handler.send(alert)

        assert result is True
        assert log_path.exists()
        content = log_path.read_text()
        assert "INFO" in content
        assert "test-pi" in content
        assert "Test alert" in content

    def test_creates_log_directory(self, tmp_path):
        """Test that handler creates log directory if missing."""
        log_path = tmp_path / "nested" / "dir" / "alerts.log"
        LogAlertHandler(log_path)  # Creating handler should create directory

        assert log_path.parent.exists()


class TestAlertManager:
    """Tests for AlertManager class."""

    def test_add_handler(self):
        """Test adding a handler."""
        manager = AlertManager()
        handler = MagicMock()

        manager.add_handler(handler)

        assert handler in manager._handlers

    def test_trigger_alert(self):
        """Test triggering an alert."""
        manager = AlertManager()
        handler1 = MagicMock()
        handler1.send.return_value = True
        handler2 = MagicMock()
        handler2.send.return_value = True

        manager.add_handler(handler1)
        manager.add_handler(handler2)

        alert = Alert(
            level=AlertLevel.WARNING,
            sbc_name="test",
            message="Test",
        )

        count = manager.trigger(alert)

        assert count == 2
        handler1.send.assert_called_once_with(alert)
        handler2.send.assert_called_once_with(alert)

    def test_trigger_convenience_methods(self):
        """Test convenience trigger methods."""
        manager = AlertManager()
        handler = MagicMock()
        handler.send.return_value = True
        manager.add_handler(handler)

        manager.trigger_info("sbc1", "Info message")
        manager.trigger_warning("sbc2", "Warning message")
        manager.trigger_critical("sbc3", "Critical message")

        assert handler.send.call_count == 3

    def test_close_handlers(self):
        """Test closing all handlers."""
        manager = AlertManager()
        handler1 = MagicMock()
        handler2 = MagicMock()

        manager.add_handler(handler1)
        manager.add_handler(handler2)
        manager.close()

        handler1.close.assert_called_once()
        handler2.close.assert_called_once()
        assert len(manager._handlers) == 0


class TestHealthConfig:
    """Tests for HealthConfig in config module."""

    def test_default_health_config(self):
        """Test default health configuration."""
        from labctl.core.config import HealthConfig

        config = HealthConfig()

        assert config.check_interval == 60
        assert config.ping_timeout == 2.0
        assert config.serial_timeout == 2.0
        assert config.status_retention_days == 30
        assert config.alert_on_offline is True
        assert config.alert_on_power_change is True

    def test_health_config_in_main_config(self):
        """Test health config is part of main config."""
        from labctl.core.config import Config

        config = Config()

        assert hasattr(config, "health")
        assert config.health.check_interval == 60

    def test_health_config_from_dict(self):
        """Test loading health config from dictionary."""
        from labctl.core.config import Config

        data = {
            "health": {
                "check_interval": 30,
                "ping_timeout": 5.0,
                "alert_on_offline": False,
            }
        }

        config = Config.from_dict(data)

        assert config.health.check_interval == 30
        assert config.health.ping_timeout == 5.0
        assert config.health.alert_on_offline is False

    def test_health_config_to_dict(self):
        """Test converting health config to dictionary."""
        from labctl.core.config import Config

        config = Config()
        data = config.to_dict()

        assert "health" in data
        assert data["health"]["check_interval"] == 60
        assert data["health"]["ping_timeout"] == 2.0
