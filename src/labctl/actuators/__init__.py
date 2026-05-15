"""Actuator drivers and runtime layer.

The :class:`RelayDriver` ABC defines the small protocol every concrete
driver implements (LCUS-1, Numato, etc.). The runtime layer (added in
later phases) sits between the manager's persistent state and the
driver's transport; this package is the driver-side boundary.

Use :func:`get_driver` rather than importing concrete driver modules
directly so deferred drivers aren't paid for at import time.
"""

from typing import TYPE_CHECKING

from labctl.actuators.base import (
    DriverError,
    ProbeOutcome,
    ProbeResult,
    RelayDriver,
    Transport,
    WriteOutcome,
)

if TYPE_CHECKING:
    from labctl.core.models import DriverName

__all__ = [
    "DriverError",
    "ProbeOutcome",
    "ProbeResult",
    "RelayDriver",
    "Transport",
    "WriteOutcome",
    "get_driver",
]


def get_driver(
    driver_name: "DriverName",
    *,
    expected_channel_count: int | None = None,
) -> RelayDriver:
    """Return a fresh, unopened driver instance for ``driver_name``.

    The returned driver still needs an :meth:`~RelayDriver.open` call
    with a :class:`Transport` before any I/O.

    Drivers other than ``LCUS1_SERIAL`` are placeholders in v1 and
    raise :class:`NotImplementedError` until their implementations land.
    """
    from labctl.core.models import DriverName

    if driver_name is DriverName.LCUS1_SERIAL:
        from labctl.actuators.lcus1 import Lcus1SerialDriver

        return Lcus1SerialDriver(
            expected_channel_count=expected_channel_count
        )
    raise NotImplementedError(
        f"driver {driver_name.value!r} is not implemented yet"
    )
