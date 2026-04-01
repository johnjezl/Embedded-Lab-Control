"""
SDWire SD card multiplexer support for lab controller.

Controls SDWire, SDWireC, and SDWire3 devices to switch SD cards
between DUT (target SBC) and host (dev machine) for automated flashing.
"""

from labctl.sdwire.controller import SDWireController

__all__ = ["SDWireController"]
