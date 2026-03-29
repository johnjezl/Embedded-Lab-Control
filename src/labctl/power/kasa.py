"""
Kasa power controller.

Controls TP-Link Kasa smart plugs and power strips via python-kasa library.
Supports single-outlet plugs and multi-outlet strips (e.g., HS300, KP303, EP40).
"""

import asyncio
import logging

from labctl.power.base import PowerController, PowerState

logger = logging.getLogger(__name__)


class KasaController(PowerController):
    """
    Power controller for TP-Link Kasa devices.

    Requires python-kasa package: pip install python-kasa

    Supports both single plugs (HS103, EP10, etc.) and multi-outlet
    power strips (HS300, KP303, EP40, etc.). For strips, use plug_index
    to select the specific outlet (1-based).

    Newer Kasa devices using the KLAP protocol require TP-Link cloud
    account credentials. Configure these in config.yaml under the
    'kasa' section.
    """

    def _load_credentials(self):
        """Load Kasa credentials from config, returning Credentials or None."""
        try:
            from kasa import Credentials

            from labctl.core.config import load_config

            config = load_config()
            if config.kasa.username and config.kasa.password:
                logger.debug(
                    "Loaded Kasa credentials for user: %s", config.kasa.username
                )
                return Credentials(config.kasa.username, config.kasa.password)
            else:
                logger.debug("No Kasa credentials configured")
        except Exception as e:
            logger.warning("Failed to load Kasa credentials: %s", e)
        return None

    async def _get_device(self):
        """
        Connect to device and return (root_device, target) tuple.

        root_device is the top-level device (needed for disconnect cleanup).
        target is the specific outlet to control — either the root device
        itself (for single plugs) or a child device (for strip outlets).

        On failure, ensures the device is disconnected before raising.
        """
        from kasa import Discover

        credentials = self._load_credentials()
        kwargs = {"host": self.address}
        if credentials:
            kwargs["credentials"] = credentials

        logger.debug("Discovering Kasa device at %s", self.address)
        device = await Discover.discover_single(**kwargs)

        try:
            logger.debug(
                "Updating device state for %s (%s)", self.address, device.alias
            )
            await device.update()
        except Exception:
            await device.disconnect()
            raise

        if device.children:
            idx = self.plug_index - 1  # Convert 1-based to 0-based
            if idx < 0 or idx >= len(device.children):
                await device.disconnect()
                raise RuntimeError(
                    f"Outlet index {self.plug_index} out of range "
                    f"(device has {len(device.children)} outlets)"
                )
            target = device.children[idx]
            logger.debug(
                "Selected outlet %d (%s) on strip %s",
                self.plug_index,
                target.alias,
                device.alias,
            )
            return device, target

        return device, device

    def _run(self, coro_func, action: str, retries: int = 2):
        """
        Run an async power operation with error handling and retries.

        Retries on authentication errors, which occur intermittently
        with HS300 firmware using the KLAP protocol.

        Args:
            coro_func: Async callable that takes (device, target) and performs the action.
            action: Human-readable action name for logging (e.g. "power_on").
            retries: Number of retry attempts on transient errors.

        Returns:
            The return value of coro_func.

        Raises:
            RuntimeError: On ImportError, authentication, connection, or device errors.
        """
        last_error = None
        for attempt in range(1 + retries):
            try:

                async def _exec():
                    device, target = await self._get_device()
                    try:
                        return await coro_func(device, target)
                    finally:
                        await device.disconnect()

                return asyncio.run(_exec())
            except ImportError:
                raise RuntimeError(
                    "python-kasa not installed. Install with: pip install python-kasa"
                )
            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                if attempt < retries:
                    logger.debug(
                        "Kasa %s attempt %d failed for %s[%d]: %s, retrying...",
                        action, attempt + 1, self.address, self.plug_index, e,
                    )
                    continue
                break

        error_type = type(last_error).__name__
        msg = (
            f"Kasa {action} failed for {self.address}"
            f"[{self.plug_index}]: {error_type}: {last_error}"
        )
        logger.debug(msg)
        raise RuntimeError(msg) from last_error

    def power_on(self) -> bool:
        """Turn power on."""
        logger.debug("Kasa power_on: %s[%d]", self.address, self.plug_index)

        async def _on(device, target):
            await target.turn_on()
            logger.debug("Power ON sent to %s[%d]", self.address, self.plug_index)
            return True

        return self._run(_on, "power_on")

    def power_off(self) -> bool:
        """Turn power off."""
        logger.debug("Kasa power_off: %s[%d]", self.address, self.plug_index)

        async def _off(device, target):
            await target.turn_off()
            logger.debug("Power OFF sent to %s[%d]", self.address, self.plug_index)
            return True

        return self._run(_off, "power_off")

    def get_state(self) -> PowerState:
        """Get current power state."""
        logger.debug("Kasa get_state: %s[%d]", self.address, self.plug_index)

        async def _state(device, target):
            state = PowerState.ON if target.is_on else PowerState.OFF
            logger.debug(
                "Power state for %s[%d]: %s",
                self.address,
                self.plug_index,
                state.value,
            )
            return state

        return self._run(_state, "get_state")
