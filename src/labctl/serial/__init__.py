"""
Serial port management for lab controller.

Handles ser2net configuration generation and serial port discovery.
"""

from labctl.serial.ser2net import generate_ser2net_config, Ser2NetPort

__all__ = ["generate_ser2net_config", "Ser2NetPort"]
