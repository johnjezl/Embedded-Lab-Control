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


# Default log directory for proxy sessions
DEFAULT_LOG_DIR = Path.home() / ".local" / "share" / "labctl" / "logs"

# Default alert log path
DEFAULT_ALERT_LOG = Path.home() / ".local" / "share" / "labctl" / "alerts.log"


@dataclass
class ProxyConfig:
    """Multi-client serial proxy configuration."""

    enabled: bool = True
    port_base: int = 5000
    port_range: int = 100  # Ports 5000-5099 available
    write_policy: str = "first"  # first, all, queue
    log_dir: Path = field(default_factory=lambda: DEFAULT_LOG_DIR)
    log_retention_days: int = 7
    max_clients: int = 10
    idle_timeout: int = 3600  # seconds


@dataclass
class HealthConfig:
    """Health check and monitoring configuration."""

    check_interval: int = 60  # seconds between checks
    ping_timeout: float = 2.0  # seconds
    serial_timeout: float = 2.0  # seconds
    status_retention_days: int = 30  # days to keep status history
    alert_log_path: Path = field(default_factory=lambda: DEFAULT_ALERT_LOG)
    alert_on_offline: bool = True
    alert_on_power_change: bool = True
    update_status_on_check: bool = True  # auto-update SBC status


@dataclass
class Config:
    """Main configuration for lab controller."""

    serial: SerialConfig = field(default_factory=SerialConfig)
    ser2net: Ser2NetConfig = field(default_factory=Ser2NetConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    database_path: Path = field(
        default_factory=lambda: DEFAULT_CONFIG_DIR / "labctl.db"
    )
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary."""
        serial_data = data.get("serial", {})
        ser2net_data = data.get("ser2net", {})
        proxy_data = data.get("proxy", {})
        health_data = data.get("health", {})

        serial = SerialConfig(
            dev_dir=Path(serial_data.get("dev_dir", "/dev/lab")),
            base_tcp_port=serial_data.get("base_tcp_port", 4000),
            default_baud=serial_data.get("default_baud", 115200),
        )

        ser2net = Ser2NetConfig(
            config_file=Path(ser2net_data.get("config_file", "/etc/ser2net.yaml")),
            enabled=ser2net_data.get("enabled", True),
        )

        proxy = ProxyConfig(
            enabled=proxy_data.get("enabled", True),
            port_base=proxy_data.get("port_base", 5000),
            port_range=proxy_data.get("port_range", 100),
            write_policy=proxy_data.get("write_policy", "first"),
            log_dir=Path(proxy_data.get("log_dir", str(DEFAULT_LOG_DIR))),
            log_retention_days=proxy_data.get("log_retention_days", 7),
            max_clients=proxy_data.get("max_clients", 10),
            idle_timeout=proxy_data.get("idle_timeout", 3600),
        )

        health = HealthConfig(
            check_interval=health_data.get("check_interval", 60),
            ping_timeout=health_data.get("ping_timeout", 2.0),
            serial_timeout=health_data.get("serial_timeout", 2.0),
            status_retention_days=health_data.get("status_retention_days", 30),
            alert_log_path=Path(
                health_data.get("alert_log_path", str(DEFAULT_ALERT_LOG))
            ),
            alert_on_offline=health_data.get("alert_on_offline", True),
            alert_on_power_change=health_data.get("alert_on_power_change", True),
            update_status_on_check=health_data.get("update_status_on_check", True),
        )

        return cls(
            serial=serial,
            ser2net=ser2net,
            proxy=proxy,
            health=health,
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
            "proxy": {
                "enabled": self.proxy.enabled,
                "port_base": self.proxy.port_base,
                "port_range": self.proxy.port_range,
                "write_policy": self.proxy.write_policy,
                "log_dir": str(self.proxy.log_dir),
                "log_retention_days": self.proxy.log_retention_days,
                "max_clients": self.proxy.max_clients,
                "idle_timeout": self.proxy.idle_timeout,
            },
            "health": {
                "check_interval": self.health.check_interval,
                "ping_timeout": self.health.ping_timeout,
                "serial_timeout": self.health.serial_timeout,
                "status_retention_days": self.health.status_retention_days,
                "alert_log_path": str(self.health.alert_log_path),
                "alert_on_offline": self.health.alert_on_offline,
                "alert_on_power_change": self.health.alert_on_power_change,
                "update_status_on_check": self.health.update_status_on_check,
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

    # Proxy overrides
    if "LABCTL_PROXY_ENABLED" in os.environ:
        config.proxy.enabled = os.environ["LABCTL_PROXY_ENABLED"].lower() in (
            "true",
            "1",
            "yes",
        )

    if "LABCTL_PROXY_PORT_BASE" in os.environ:
        try:
            config.proxy.port_base = int(os.environ["LABCTL_PROXY_PORT_BASE"])
        except ValueError:
            pass

    if "LABCTL_PROXY_WRITE_POLICY" in os.environ:
        policy = os.environ["LABCTL_PROXY_WRITE_POLICY"]
        if policy in ("first", "all", "queue"):
            config.proxy.write_policy = policy

    if "LABCTL_PROXY_LOG_DIR" in os.environ:
        config.proxy.log_dir = Path(os.environ["LABCTL_PROXY_LOG_DIR"])

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
