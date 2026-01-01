"""
Configuration management for lab controller.

Loads configuration from YAML files with environment variable overrides.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# Default configuration paths
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "labctl"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"
SYSTEM_CONFIG_FILE = Path("/etc/labctl/config.yaml")


@dataclass
class SerialConfig:
    """Serial port configuration."""

    dev_dir: Path = field(default_factory=lambda: Path("/dev/lab"))
    base_tcp_port: int = 4000
    default_baud: int = 115200


@dataclass
class Ser2NetConfig:
    """ser2net configuration."""

    config_file: Path = field(default_factory=lambda: Path("/etc/ser2net.yaml"))
    enabled: bool = True


@dataclass
class Config:
    """Main configuration for lab controller."""

    serial: SerialConfig = field(default_factory=SerialConfig)
    ser2net: Ser2NetConfig = field(default_factory=Ser2NetConfig)
    database_path: Path = field(
        default_factory=lambda: DEFAULT_CONFIG_DIR / "labctl.db"
    )
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary."""
        serial_data = data.get("serial", {})
        ser2net_data = data.get("ser2net", {})

        serial = SerialConfig(
            dev_dir=Path(serial_data.get("dev_dir", "/dev/lab")),
            base_tcp_port=serial_data.get("base_tcp_port", 4000),
            default_baud=serial_data.get("default_baud", 115200),
        )

        ser2net = Ser2NetConfig(
            config_file=Path(ser2net_data.get("config_file", "/etc/ser2net.yaml")),
            enabled=ser2net_data.get("enabled", True),
        )

        return cls(
            serial=serial,
            ser2net=ser2net,
            database_path=Path(
                data.get("database_path", str(DEFAULT_CONFIG_DIR / "labctl.db"))
            ),
            log_level=data.get("log_level", "INFO"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Config to dictionary."""
        return {
            "serial": {
                "dev_dir": str(self.serial.dev_dir),
                "base_tcp_port": self.serial.base_tcp_port,
                "default_baud": self.serial.default_baud,
            },
            "ser2net": {
                "config_file": str(self.ser2net.config_file),
                "enabled": self.ser2net.enabled,
            },
            "database_path": str(self.database_path),
            "log_level": self.log_level,
        }


def load_config(
    config_path: Optional[Path] = None,
    create_if_missing: bool = False,
) -> Config:
    """
    Load configuration from YAML file.

    Search order:
    1. Explicit path if provided
    2. LABCTL_CONFIG environment variable
    3. ~/.config/labctl/config.yaml
    4. /etc/labctl/config.yaml
    5. Default values

    Environment variable overrides:
    - LABCTL_DEV_DIR: Override serial.dev_dir
    - LABCTL_BASE_TCP_PORT: Override serial.base_tcp_port
    - LABCTL_DATABASE_PATH: Override database_path
    - LABCTL_LOG_LEVEL: Override log_level

    Args:
        config_path: Optional explicit path to config file
        create_if_missing: Create default config if no config found

    Returns:
        Loaded configuration
    """
    # Determine config file path
    if config_path:
        paths_to_try = [config_path]
    else:
        env_path = os.environ.get("LABCTL_CONFIG")
        paths_to_try = []
        if env_path:
            paths_to_try.append(Path(env_path))
        paths_to_try.extend([DEFAULT_CONFIG_FILE, SYSTEM_CONFIG_FILE])

    # Try to load from file
    config_data = {}
    for path in paths_to_try:
        if path.exists():
            try:
                with open(path) as f:
                    config_data = yaml.safe_load(f) or {}
                break
            except Exception:
                continue

    # Create config from loaded data (or defaults)
    config = Config.from_dict(config_data)

    # Apply environment variable overrides
    config = _apply_env_overrides(config)

    # Create default config file if requested and none exists
    if create_if_missing and not any(p.exists() for p in paths_to_try):
        save_config(config, DEFAULT_CONFIG_FILE)

    return config


def _apply_env_overrides(config: Config) -> Config:
    """Apply environment variable overrides to config."""
    if "LABCTL_DEV_DIR" in os.environ:
        config.serial.dev_dir = Path(os.environ["LABCTL_DEV_DIR"])

    if "LABCTL_BASE_TCP_PORT" in os.environ:
        try:
            config.serial.base_tcp_port = int(os.environ["LABCTL_BASE_TCP_PORT"])
        except ValueError:
            pass

    if "LABCTL_DATABASE_PATH" in os.environ:
        config.database_path = Path(os.environ["LABCTL_DATABASE_PATH"])

    if "LABCTL_LOG_LEVEL" in os.environ:
        config.log_level = os.environ["LABCTL_LOG_LEVEL"]

    return config


def save_config(config: Config, path: Path) -> None:
    """
    Save configuration to YAML file.

    Args:
        config: Configuration to save
        path: Path to save to
    """
    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write config with header comment
    with open(path, "w") as f:
        f.write("# Lab Controller Configuration\n")
        f.write("# See documentation for all options\n\n")
        yaml.dump(config.to_dict(), f, default_flow_style=False, sort_keys=False)


def get_default_config() -> Config:
    """Get default configuration without loading from file."""
    return Config()


def ensure_config_dir() -> Path:
    """Ensure config directory exists and return its path."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CONFIG_DIR
