"""
Power control module for lab controller.

Provides smart plug control for Tasmota, Kasa, and Shelly devices.
"""

from labctl.power.base import PowerController, PowerState, get_controller

__all__ = [
    "PowerController",
    "PowerState",
    "get_controller",
]
