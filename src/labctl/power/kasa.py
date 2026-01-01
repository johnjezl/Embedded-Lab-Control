"""
Kasa power controller.

Controls TP-Link Kasa smart plugs via python-kasa library.
"""

from labctl.power.base import PowerController, PowerState


class KasaController(PowerController):
    """
    Power controller for TP-Link Kasa devices.

    Requires python-kasa package: pip install python-kasa

    Note: This is a stub implementation. Full async implementation
    requires python-kasa library and async handling.
    """

    def power_on(self) -> bool:
        """Turn power on."""
        try:
            import asyncio
            from kasa import SmartPlug

            async def _on():
                plug = SmartPlug(self.address)
                await plug.update()
                await plug.turn_on()
                return True

            return asyncio.run(_on())
        except ImportError:
            raise RuntimeError(
                "python-kasa not installed. Install with: pip install python-kasa"
            )
        except Exception:
            return False

    def power_off(self) -> bool:
        """Turn power off."""
        try:
            import asyncio
            from kasa import SmartPlug

            async def _off():
                plug = SmartPlug(self.address)
                await plug.update()
                await plug.turn_off()
                return True

            return asyncio.run(_off())
        except ImportError:
            raise RuntimeError(
                "python-kasa not installed. Install with: pip install python-kasa"
            )
        except Exception:
            return False

    def get_state(self) -> PowerState:
        """Get current power state."""
        try:
            import asyncio
            from kasa import SmartPlug

            async def _state():
                plug = SmartPlug(self.address)
                await plug.update()
                return PowerState.ON if plug.is_on else PowerState.OFF

            return asyncio.run(_state())
        except ImportError:
            raise RuntimeError(
                "python-kasa not installed. Install with: pip install python-kasa"
            )
        except Exception:
            return PowerState.UNKNOWN
