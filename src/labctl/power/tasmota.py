"""
Tasmota power controller.

Controls Tasmota-flashed smart plugs via HTTP API.
"""

import requests
from typing import Optional

from labctl.power.base import PowerController, PowerState


class TasmotaController(PowerController):
    """
    Power controller for Tasmota devices.

    Uses Tasmota HTTP API:
    - Power On:  http://<ip>/cm?cmnd=Power<index>%20On
    - Power Off: http://<ip>/cm?cmnd=Power<index>%20Off
    - Status:    http://<ip>/cm?cmnd=Power<index>
    """

    def _command(self, cmnd: str) -> Optional[dict]:
        """
        Send command to Tasmota device.

        Args:
            cmnd: Command string (e.g., "Power1 On")

        Returns:
            JSON response dict or None on error
        """
        url = f"http://{self.address}/cm"
        params = {"cmnd": cmnd}

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def _power_key(self) -> str:
        """Get the power key for responses (POWER or POWER1, POWER2, etc.)."""
        if self.plug_index == 1:
            return "POWER"
        return f"POWER{self.plug_index}"

    def power_on(self) -> bool:
        """Turn power on."""
        cmnd = f"Power{self.plug_index} On"
        result = self._command(cmnd)

        if result is None:
            return False

        # Tasmota returns {"POWER": "ON"} or {"POWER1": "ON"}
        power_key = self._power_key()
        return result.get(power_key, "").upper() == "ON"

    def power_off(self) -> bool:
        """Turn power off."""
        cmnd = f"Power{self.plug_index} Off"
        result = self._command(cmnd)

        if result is None:
            return False

        power_key = self._power_key()
        return result.get(power_key, "").upper() == "OFF"

    def get_state(self) -> PowerState:
        """Get current power state."""
        cmnd = f"Power{self.plug_index}"
        result = self._command(cmnd)

        if result is None:
            return PowerState.UNKNOWN

        power_key = self._power_key()
        state = result.get(power_key, "").upper()

        if state == "ON":
            return PowerState.ON
        elif state == "OFF":
            return PowerState.OFF
        else:
            return PowerState.UNKNOWN
