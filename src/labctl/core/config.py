"""
Configuration management for lab controller.

Loads configuration from YAML files with environment variable overrides.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

import yaml

SYSTEM_CONFIG_FILE = Path("/etc/labctl/config.yaml")


def _default_config_dir() -> Path:
    """Return the user config dir, honoring XDG_CONFIG_HOME when set."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "labctl"
    return Path.home() / ".config" / "labctl"


def _default_config_file() -> Path:
    """Return the default config file path for the current user."""
    return _default_config_dir() / "config.yaml"


def _default_data_dir() -> Path:
    """Return the user data dir, honoring XDG_DATA_HOME when set."""
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser() / "labctl"
    return Path.home() / ".local" / "share" / "labctl"


def _expand_path(value: str | Path) -> Path:
    """Expand env vars and ~ in config-provided path values."""
    return Path(os.path.expandvars(str(value))).expanduser()


def _path_exists(path: Path) -> bool:
    """Best-effort existence check that tolerates unreadable paths."""
    try:
        return path.exists()
    except OSError as e:
        logger.warning("Failed to access config path %s: %s", path, e)
        return False


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
class ProxyConfig:
    """Multi-client serial proxy configuration."""

    enabled: bool = True
    port_base: int = 5500
    port_range: int = 100  # Ports 5500-5599 available
    write_policy: str = "first"  # first, all, queue
    log_dir: Path = field(default_factory=lambda: _default_data_dir() / "logs")
    log_retention_days: int = 7
    max_clients: int = 10
    idle_timeout: int = 3600  # seconds


@dataclass
class HealthConfig:
    """Health check and monitoring configuration.

    The daemon runs two cadences:
      - Fast track (`check_interval`): ping + serial probe.
      - Slow track (`power_check_interval`): power probe (network-bound,
        sometimes slow). Power runs on every Nth fast tick where
        N = ceil(power_check_interval / check_interval).

    `min_sleep_seconds` is the floor on between-cycle sleep so a runaway
    cycle can't pin a CPU spinning when elapsed >= check_interval.
    """

    check_interval: int = 10  # seconds between fast (ping+serial) cycles
    power_check_interval: int = 60  # seconds between power probes
    min_sleep_seconds: float = 1.0  # floor on between-cycle sleep
    ping_timeout: float = 2.0  # seconds
    serial_timeout: float = 2.0  # seconds
    status_retention_days: int = 30  # days to keep status history
    alert_log_path: Path = field(
        default_factory=lambda: _default_data_dir() / "alerts.log"
    )
    alert_on_offline: bool = True
    alert_on_power_change: bool = True
    update_status_on_check: bool = True  # auto-update SBC status


@dataclass
class WebConfig:
    """Web server SSL/TLS configuration."""

    cert_file: str = ""
    key_file: str = ""


@dataclass
class KasaConfig:
    """TP-Link Kasa device credentials.

    Newer Kasa devices (KLAP protocol) require TP-Link cloud account
    credentials for local control.
    """

    username: str = ""
    password: str = ""


@dataclass
class UserConfig:
    """User authentication configuration."""

    username: str = ""
    password_hash: str = ""
    api_key: str = ""


@dataclass
class AuthConfig:
    """Authentication configuration."""

    enabled: bool = False
    users: list[UserConfig] = field(default_factory=list)
    secret_key: str = ""
    session_lifetime_minutes: int = 480


@dataclass
class ClaimsConfig:
    """Hardware claim (exclusive access) configuration."""

    enabled: bool = True
    default_duration_minutes: int = 30
    max_duration_minutes: int = 1440  # 24h — covers overnight reliability runs
    min_duration_minutes: int = 1
    grace_period_seconds: int = 60
    auto_prune_released_after_days: int = 30
    require_agent_name: bool = False

    def validate(self) -> list[str]:
        """Check invariants, returning a list of warning messages.

        Clamps obviously wrong values to sane defaults so the system
        still functions. Call after construction.
        """
        warnings = []
        if self.min_duration_minutes < 1:
            warnings.append(
                f"claims.min_duration_minutes={self.min_duration_minutes}"
                " clamped to 1"
            )
            self.min_duration_minutes = 1
        if self.max_duration_minutes < self.min_duration_minutes:
            warnings.append(
                f"claims.max_duration_minutes={self.max_duration_minutes}"
                f" < min ({self.min_duration_minutes}), clamped to min"
            )
            self.max_duration_minutes = self.min_duration_minutes
        if (
            self.default_duration_minutes < self.min_duration_minutes
            or self.default_duration_minutes > self.max_duration_minutes
        ):
            clamped = max(
                self.min_duration_minutes,
                min(self.default_duration_minutes, self.max_duration_minutes),
            )
            warnings.append(
                f"claims.default_duration_minutes="
                f"{self.default_duration_minutes}"
                f" outside [{self.min_duration_minutes},"
                f" {self.max_duration_minutes}], clamped to {clamped}"
            )
            self.default_duration_minutes = clamped
        if self.grace_period_seconds < 0:
            warnings.append("claims.grace_period_seconds < 0, clamped to 0")
            self.grace_period_seconds = 0
        if self.auto_prune_released_after_days < 1:
            warnings.append("claims.auto_prune_released_after_days < 1, clamped to 1")
            self.auto_prune_released_after_days = 1
        return warnings


@dataclass
class Config:
    """Main configuration for lab controller."""

    serial: SerialConfig = field(default_factory=SerialConfig)
    ser2net: Ser2NetConfig = field(default_factory=Ser2NetConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    web: WebConfig = field(default_factory=WebConfig)
    kasa: KasaConfig = field(default_factory=KasaConfig)
    claims: ClaimsConfig = field(default_factory=ClaimsConfig)
    database_path: Path = field(
        default_factory=lambda: _default_config_dir() / "labctl.db"
    )
    log_level: str = "WARNING"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary."""
        serial_data = data.get("serial", {})
        ser2net_data = data.get("ser2net", {})
        proxy_data = data.get("proxy", {})
        health_data = data.get("health", {})
        auth_data = data.get("auth", {})
        web_data = data.get("web", {})
        kasa_data = data.get("kasa", {})
        claims_data = data.get("claims", {})

        serial = SerialConfig(
            dev_dir=_expand_path(serial_data.get("dev_dir", "/dev/lab")),
            base_tcp_port=serial_data.get("base_tcp_port", 4000),
            default_baud=serial_data.get("default_baud", 115200),
        )

        ser2net = Ser2NetConfig(
            config_file=_expand_path(
                ser2net_data.get("config_file", "/etc/ser2net.yaml")
            ),
            enabled=ser2net_data.get("enabled", True),
        )

        proxy = ProxyConfig(
            enabled=proxy_data.get("enabled", True),
            port_base=proxy_data.get("port_base", 5500),
            port_range=proxy_data.get("port_range", 100),
            write_policy=proxy_data.get("write_policy", "first"),
            log_dir=_expand_path(
                proxy_data.get("log_dir", str(_default_data_dir() / "logs"))
            ),
            log_retention_days=proxy_data.get("log_retention_days", 7),
            max_clients=proxy_data.get("max_clients", 10),
            idle_timeout=proxy_data.get("idle_timeout", 3600),
        )

        health = HealthConfig(
            check_interval=health_data.get("check_interval", 10),
            power_check_interval=health_data.get("power_check_interval", 60),
            min_sleep_seconds=health_data.get("min_sleep_seconds", 1.0),
            ping_timeout=health_data.get("ping_timeout", 2.0),
            serial_timeout=health_data.get("serial_timeout", 2.0),
            status_retention_days=health_data.get("status_retention_days", 30),
            alert_log_path=_expand_path(
                health_data.get(
                    "alert_log_path",
                    str(_default_data_dir() / "alerts.log"),
                )
            ),
            alert_on_offline=health_data.get("alert_on_offline", True),
            alert_on_power_change=health_data.get("alert_on_power_change", True),
            update_status_on_check=health_data.get("update_status_on_check", True),
        )

        auth_users = []
        for u in auth_data.get("users", []):
            auth_users.append(
                UserConfig(
                    username=u.get("username", ""),
                    password_hash=u.get("password_hash", ""),
                    api_key=u.get("api_key", ""),
                )
            )

        auth = AuthConfig(
            enabled=auth_data.get("enabled", False),
            users=auth_users,
            secret_key=auth_data.get("secret_key", ""),
            session_lifetime_minutes=auth_data.get("session_lifetime_minutes", 480),
        )

        web = WebConfig(
            cert_file=web_data.get("cert_file", ""),
            key_file=web_data.get("key_file", ""),
        )

        kasa = KasaConfig(
            username=kasa_data.get("username", ""),
            password=kasa_data.get("password", ""),
        )

        claims = ClaimsConfig(
            enabled=claims_data.get("enabled", True),
            default_duration_minutes=claims_data.get("default_duration_minutes", 30),
            max_duration_minutes=claims_data.get("max_duration_minutes", 1440),
            min_duration_minutes=claims_data.get("min_duration_minutes", 1),
            grace_period_seconds=claims_data.get("grace_period_seconds", 60),
            auto_prune_released_after_days=claims_data.get(
                "auto_prune_released_after_days", 30
            ),
            require_agent_name=claims_data.get("require_agent_name", False),
        )

        # Validate and clamp claim config bounds
        claim_warnings = claims.validate()
        for w in claim_warnings:
            logger.warning("Config: %s", w)

        return cls(
            serial=serial,
            ser2net=ser2net,
            proxy=proxy,
            health=health,
            auth=auth,
            web=web,
            kasa=kasa,
            claims=claims,
            database_path=_expand_path(
                data.get("database_path", str(_default_config_dir() / "labctl.db"))
            ),
            log_level=data.get("log_level", "WARNING"),
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
                "power_check_interval": self.health.power_check_interval,
                "min_sleep_seconds": self.health.min_sleep_seconds,
                "ping_timeout": self.health.ping_timeout,
                "serial_timeout": self.health.serial_timeout,
                "status_retention_days": self.health.status_retention_days,
                "alert_log_path": str(self.health.alert_log_path),
                "alert_on_offline": self.health.alert_on_offline,
                "alert_on_power_change": self.health.alert_on_power_change,
                "update_status_on_check": self.health.update_status_on_check,
            },
            "auth": {
                "enabled": self.auth.enabled,
                "users": [
                    {
                        "username": u.username,
                        "password_hash": u.password_hash,
                        "api_key": u.api_key,
                    }
                    for u in self.auth.users
                ],
                "secret_key": self.auth.secret_key,
                "session_lifetime_minutes": self.auth.session_lifetime_minutes,
            },
            "web": {
                "cert_file": self.web.cert_file,
                "key_file": self.web.key_file,
            },
            "kasa": {
                "username": self.kasa.username,
                "password": self.kasa.password,
            },
            "claims": {
                "enabled": self.claims.enabled,
                "default_duration_minutes": self.claims.default_duration_minutes,
                "max_duration_minutes": self.claims.max_duration_minutes,
                "min_duration_minutes": self.claims.min_duration_minutes,
                "grace_period_seconds": self.claims.grace_period_seconds,
                "auto_prune_released_after_days": self.claims.auto_prune_released_after_days,
                "require_agent_name": self.claims.require_agent_name,
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
            paths_to_try.append(_expand_path(env_path))
        paths_to_try.extend([_default_config_file(), SYSTEM_CONFIG_FILE])

    # Try to load from file
    config_data = {}
    for path in paths_to_try:
        if _path_exists(path):
            try:
                with open(path) as f:
                    config_data = yaml.safe_load(f) or {}
                break
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", path, e)
                continue

    # Create config from loaded data (or defaults)
    config = Config.from_dict(config_data)

    # Apply environment variable overrides
    config = _apply_env_overrides(config)

    # Create default config file if requested and none exists
    if create_if_missing and not any(_path_exists(p) for p in paths_to_try):
        save_config(config, _default_config_file())

    return config


def _apply_env_overrides(config: Config) -> Config:
    """Apply environment variable overrides to config."""
    if "LABCTL_DEV_DIR" in os.environ:
        config.serial.dev_dir = _expand_path(os.environ["LABCTL_DEV_DIR"])

    if "LABCTL_BASE_TCP_PORT" in os.environ:
        try:
            config.serial.base_tcp_port = int(os.environ["LABCTL_BASE_TCP_PORT"])
        except ValueError:
            pass

    if "LABCTL_DATABASE_PATH" in os.environ:
        config.database_path = _expand_path(os.environ["LABCTL_DATABASE_PATH"])

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
        config.proxy.log_dir = _expand_path(os.environ["LABCTL_PROXY_LOG_DIR"])

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
    config_dir = _default_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir
