"""Unit tests for configuration management."""

from pathlib import Path

from labctl.core.config import (
    Config,
    Ser2NetConfig,
    SerialConfig,
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


class TestConfig:
    """Tests for main Config class."""

    def test_default_values(self):
        """Test default configuration values."""
        config = Config()
        assert isinstance(config.serial, SerialConfig)
        assert isinstance(config.ser2net, Ser2NetConfig)
        assert config.log_level == "INFO"

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
