"""
Shelly power controller.

Controls Shelly smart plugs via HTTP API.
"""

from typing import Optional

import requests

from labctl.power.base import PowerController, PowerState


class ShellyController(PowerController):
    """
    Power controller for Shelly devices.

    Uses Shelly HTTP API (Gen1):
    - Power On:  http://<ip>/relay/<index>?turn=on
    - Power Off: http://<ip>/relay/<index>?turn=off
    - Status:    http://<ip>/relay/<index>

    Note: plug_index is 0-based for Shelly API (converted from 1-based input)
    """

    @property
    def _relay_index(self) -> int:
        """Convert 1-based plug_index to 0-based relay index."""
        return self.plug_index - 1

    def _request(self, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
        """
        Send request to Shelly device.

        Args:
            endpoint: API endpoint (e.g., "relay/0")
            params: Optional query parameters

        Returns:
            JSON response dict or None on error
        """
        url = f"http://{self.address}/{endpoint}"

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def power_on(self) -> bool:
        """Turn power on."""
        result = self._request(f"relay/{self._relay_index}", params={"turn": "on"})

        if result is None:
            return False

        # Shelly returns {"ison": true, ...}
        return result.get("ison", False) is True

    def power_off(self) -> bool:
        """Turn power off."""
        result = self._request(f"relay/{self._relay_index}", params={"turn": "off"})

        if result is None:
            return False

        return result.get("ison", True) is False

    def get_state(self) -> PowerState:
        """Get current power state."""
        result = self._request(f"relay/{self._relay_index}")

        if result is None:
            return PowerState.UNKNOWN

        ison = result.get("ison")
        if ison is True:
            return PowerState.ON
        elif ison is False:
            return PowerState.OFF
        else:
            return PowerState.UNKNOWN
