"""Actuator drivers and runtime layer.

The :class:`RelayDriver` ABC defines the small protocol every concrete
driver implements (LCUS-1, Numato, etc.). The runtime layer (added in
later phases) sits between the manager's persistent state and the
driver's transport; this package is the driver-side boundary.
"""

from labctl.actuators.base import (
    DriverError,
    ProbeOutcome,
    ProbeResult,
    RelayDriver,
    Transport,
    WriteOutcome,
)

__all__ = [
    "DriverError",
    "ProbeOutcome",
    "ProbeResult",
    "RelayDriver",
    "Transport",
    "WriteOutcome",
]
