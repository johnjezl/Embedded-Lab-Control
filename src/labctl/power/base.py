"""
Base classes for power controllers.

Defines abstract interface and factory for smart plug implementations.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labctl.core.models import PowerPlug, PlugType


class PowerState(Enum):
    """Power state values."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class PowerController(ABC):
    """Abstract base class for power controllers."""

    def __init__(self, address: str, plug_index: int = 1, timeout: float = 5.0):
        """
        Initialize power controller.

        Args:
            address: IP address or hostname of the device
            plug_index: Outlet index for multi-relay devices (1-based)
            timeout: Request timeout in seconds
        """
        self.address = address
        self.plug_index = plug_index
        self.timeout = timeout

    @abstractmethod
    def power_on(self) -> bool:
        """
        Turn power on.

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def power_off(self) -> bool:
        """
        Turn power off.

        Returns:
            True if successful, False otherwise
        """
        pass

    def power_cycle(self, delay: float = 2.0) -> bool:
        """
        Power cycle (off, wait, on).

        Args:
            delay: Seconds to wait between off and on

        Returns:
            True if successful, False otherwise
        """
        import time

        if not self.power_off():
            return False
        time.sleep(delay)
        return self.power_on()

    @abstractmethod
    def get_state(self) -> PowerState:
        """
        Get current power state.

        Returns:
            PowerState enum value
        """
        pass

    @classmethod
    def from_plug(cls, plug: "PowerPlug", timeout: float = 5.0) -> "PowerController":
        """
        Create controller from PowerPlug model.

        Args:
            plug: PowerPlug instance from database
            timeout: Request timeout in seconds

        Returns:
            Appropriate PowerController subclass instance
        """
        return get_controller(
            plug_type=plug.plug_type,
            address=plug.address,
            plug_index=plug.plug_index,
            timeout=timeout,
        )


def get_controller(
    plug_type: "PlugType",
    address: str,
    plug_index: int = 1,
    timeout: float = 5.0,
) -> PowerController:
    """
    Factory function to create appropriate power controller.

    Args:
        plug_type: Type of smart plug (tasmota, kasa, shelly)
        address: IP address or hostname
        plug_index: Outlet index for multi-relay devices
        timeout: Request timeout in seconds

    Returns:
        PowerController subclass instance

    Raises:
        ValueError: If plug_type is not supported
    """
    from labctl.core.models import PlugType

    if plug_type == PlugType.TASMOTA:
        from labctl.power.tasmota import TasmotaController
        return TasmotaController(address, plug_index, timeout)

    elif plug_type == PlugType.KASA:
        from labctl.power.kasa import KasaController
        return KasaController(address, plug_index, timeout)

    elif plug_type == PlugType.SHELLY:
        from labctl.power.shelly import ShellyController
        return ShellyController(address, plug_index, timeout)

    else:
        raise ValueError(f"Unsupported plug type: {plug_type}")
