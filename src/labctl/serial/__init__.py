"""
Serial port management for lab controller.

Handles ser2net configuration generation, serial port discovery,
and multi-client serial proxying.
"""

from labctl.serial.proxy import ProxyClient, ProxyManager, SerialProxy, SessionLogger
from labctl.serial.ser2net import Ser2NetPort, generate_ser2net_config

__all__ = [
    "generate_ser2net_config",
    "Ser2NetPort",
    "SerialProxy",
    "ProxyManager",
    "ProxyClient",
    "SessionLogger",
]
