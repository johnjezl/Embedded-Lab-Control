"""Unit tests for configuration management."""

import logging
from pathlib import Path

from labctl.core.config import (
    ClaimsConfig,
    Config,
    KasaConfig,
    Ser2NetConfig,
    SerialConfig,
    WebConfig,
    get_default_config,
    load_config,
    save_config,
)


class TestSerialConfig:
    """Tests for SerialConfig dataclass."""

    def test_default_values(self):
        """Test default serial configuration."""
        config = SerialConfig()
        assert config.dev_dir == Path("/dev/lab")
        assert config.base_tcp_port == 4000
        assert config.default_baud == 115200


class TestSer2NetConfig:
    """Tests for Ser2NetConfig dataclass."""

    def test_default_values(self):
        """Test default ser2net configuration."""
        config = Ser2NetConfig()
        assert config.config_file == Path("/etc/ser2net.yaml")
        assert config.enabled is True


class TestWebConfig:
    """Tests for WebConfig dataclass."""

    def test_default_values(self):
        """Test default web configuration."""
        config = WebConfig()
        assert config.cert_file == ""
        assert config.key_file == ""

    def test_from_dict(self):
        """Test WebConfig values populated from Config.from_dict."""
        data = {
            "web": {
                "cert_file": "/etc/ssl/cert.pem",
                "key_file": "/etc/ssl/key.pem",
            }
        }
        config = Config.from_dict(data)
        assert config.web.cert_file == "/etc/ssl/cert.pem"
        assert config.web.key_file == "/etc/ssl/key.pem"

    def test_to_dict(self):
        """Test WebConfig values appear in Config.to_dict."""
        config = Config()
        config.web.cert_file = "/path/to/cert"
        config.web.key_file = "/path/to/key"
        data = config.to_dict()

        assert data["web"]["cert_file"] == "/path/to/cert"
        assert data["web"]["key_file"] == "/path/to/key"

    def test_roundtrip(self):
        """Test WebConfig survives to_dict/from_dict roundtrip."""
        original = Config()
        original.web.cert_file = "/etc/ssl/lab.crt"
        original.web.key_file = "/etc/ssl/lab.key"
        data = original.to_dict()
        restored = Config.from_dict(data)

        assert restored.web.cert_file == original.web.cert_file
        assert restored.web.key_file == original.web.key_file


class TestKasaConfig:
    """Tests for KasaConfig dataclass."""

    def test_default_values(self):
        """Test default kasa configuration."""
        config = KasaConfig()
        assert config.username == ""
        assert config.password == ""

    def test_from_dict(self):
        """Test KasaConfig values populated from Config.from_dict."""
        data = {
            "kasa": {
                "username": "user@example.com",
                "password": "secret123",
            }
        }
        config = Config.from_dict(data)
        assert config.kasa.username == "user@example.com"
        assert config.kasa.password == "secret123"


class TestClaimsConfig:
    """Tests for ClaimsConfig dataclass."""

    def test_default_values(self):
        config = ClaimsConfig()
        assert config.enabled is True
        assert config.default_duration_minutes == 30
        assert config.max_duration_minutes == 1440
        assert config.min_duration_minutes == 1
        assert config.grace_period_seconds == 60
        assert config.auto_prune_released_after_days == 30
        assert config.require_agent_name is False

    def test_from_dict(self):
        data = {
            "claims": {
                "enabled": False,
                "default_duration_minutes": 15,
                "max_duration_minutes": 120,
                "grace_period_seconds": 30,
                "require_agent_name": True,
            }
        }
        config = Config.from_dict(data)
        assert config.claims.enabled is False
        assert config.claims.default_duration_minutes == 15
        assert config.claims.max_duration_minutes == 120
        assert config.claims.grace_period_seconds == 30
        assert config.claims.require_agent_name is True

    def test_roundtrip(self):
        original = Config()
        original.claims.enabled = False
        original.claims.max_duration_minutes = 720
        original.claims.require_agent_name = True
        restored = Config.from_dict(original.to_dict())
        assert restored.claims.enabled is False
        assert restored.claims.max_duration_minutes == 720
        assert restored.claims.require_agent_name is True


class TestConfig:
    """Tests for main Config class."""

    def test_default_values(self):
        """Test default configuration values."""
        config = Config()
        assert isinstance(config.serial, SerialConfig)
        assert isinstance(config.ser2net, Ser2NetConfig)
        assert config.log_level == "WARNING"

    def test_from_dict_empty(self):
        """Test creating config from empty dict uses defaults."""
        config = Config.from_dict({})
        assert config.serial.dev_dir == Path("/dev/lab")
        assert config.serial.base_tcp_port == 4000

    def test_from_dict_custom_values(self):
        """Test creating config from dict with custom values."""
        data = {
            "serial": {
                "dev_dir": "/custom/dev",
                "base_tcp_port": 5000,
                "default_baud": 9600,
            },
            "ser2net": {
                "config_file": "/custom/ser2net.yaml",
                "enabled": False,
            },
            "database_path": "/custom/db.sqlite",
            "log_level": "DEBUG",
        }
        config = Config.from_dict(data)

        assert config.serial.dev_dir == Path("/custom/dev")
        assert config.serial.base_tcp_port == 5000
        assert config.serial.default_baud == 9600
        assert config.ser2net.config_file == Path("/custom/ser2net.yaml")
        assert config.ser2net.enabled is False
        assert config.database_path == Path("/custom/db.sqlite")
        assert config.log_level == "DEBUG"

    def test_to_dict(self):
        """Test converting config to dict."""
        config = Config()
        data = config.to_dict()

        assert "serial" in data
        assert "ser2net" in data
        assert "database_path" in data
        assert "log_level" in data
        assert data["serial"]["dev_dir"] == "/dev/lab"

    def test_roundtrip(self):
        """Test config survives to_dict/from_dict roundtrip."""
        original = Config()
        data = original.to_dict()
        restored = Config.from_dict(data)

        assert restored.serial.dev_dir == original.serial.dev_dir
        assert restored.serial.base_tcp_port == original.serial.base_tcp_port
        assert restored.ser2net.enabled == original.ser2net.enabled


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_default_config(self):
        """Test loading config with no file returns defaults."""
        config = load_config()
        assert isinstance(config, Config)
        assert config.serial.dev_dir == Path("/dev/lab")

    def test_load_from_explicit_path(self, tmp_path):
        """Test loading config from explicit path."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
serial:
  base_tcp_port: 6000
log_level: WARNING
"""
        )
        config = load_config(config_file)
        assert config.serial.base_tcp_port == 6000
        assert config.log_level == "WARNING"

    def test_env_override_dev_dir(self, monkeypatch):
        """Test LABCTL_DEV_DIR environment override."""
        monkeypatch.setenv("LABCTL_DEV_DIR", "/custom/dev/path")
        config = load_config()
        assert config.serial.dev_dir == Path("/custom/dev/path")

    def test_env_override_tcp_port(self, monkeypatch):
        """Test LABCTL_BASE_TCP_PORT environment override."""
        monkeypatch.setenv("LABCTL_BASE_TCP_PORT", "7000")
        config = load_config()
        assert config.serial.base_tcp_port == 7000

    def test_env_override_log_level(self, monkeypatch):
        """Test LABCTL_LOG_LEVEL environment override."""
        monkeypatch.setenv("LABCTL_LOG_LEVEL", "DEBUG")
        config = load_config()
        assert config.log_level == "DEBUG"

    def test_env_override_invalid_port(self, monkeypatch):
        """Test invalid LABCTL_BASE_TCP_PORT is ignored."""
        monkeypatch.setenv("LABCTL_BASE_TCP_PORT", "not-a-number")
        config = load_config()
        assert config.serial.base_tcp_port == 4000  # Default


class TestSaveConfig:
    """Tests for save_config function."""

    def test_save_config(self, tmp_path):
        """Test saving config to file."""
        config = Config()
        config_file = tmp_path / "subdir" / "config.yaml"

        save_config(config, config_file)

        assert config_file.exists()
        content = config_file.read_text()
        assert "serial:" in content
        assert "dev_dir:" in content

    def test_save_and_load_roundtrip(self, tmp_path):
        """Test config survives save/load roundtrip."""
        original = Config()
        original.serial.base_tcp_port = 8000
        original.log_level = "ERROR"

        config_file = tmp_path / "config.yaml"
        save_config(original, config_file)
        loaded = load_config(config_file)

        assert loaded.serial.base_tcp_port == 8000
        assert loaded.log_level == "ERROR"


class TestGetDefaultConfig:
    """Tests for get_default_config function."""

    def test_returns_config(self):
        """Test get_default_config returns Config instance."""
        config = get_default_config()
        assert isinstance(config, Config)

    def test_returns_defaults(self):
        """Test get_default_config returns default values."""
        config = get_default_config()
        assert config.serial.dev_dir == Path("/dev/lab")
        assert config.serial.base_tcp_port == 4000


class TestConfigLoadLogging:
    """Tests for config load error logging."""

    def test_invalid_yaml_logs_warning(self, tmp_path, caplog):
        """Test that invalid YAML logs a warning and falls back."""
        bad_config = tmp_path / "labctl.yaml"
        bad_config.write_text("{{invalid yaml::: [")

        with caplog.at_level(logging.WARNING, logger="labctl.core.config"):
            config = load_config(bad_config)

        assert any("Failed to load config" in r.message for r in caplog.records)
        # Should return defaults since the config file failed
        assert isinstance(config, Config)
