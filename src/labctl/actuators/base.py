"""Base classes for relay/actuator drivers.

Every concrete driver (LCUS-1, Numato, HID, sysfs-GPIO, …) implements
:class:`RelayDriver`. The runtime layer never imports a concrete
driver directly — it asks the registry for a driver matching the
:class:`labctl.core.models.DriverName` recorded on the actuator row.

Outcome enums (:class:`WriteOutcome`, :class:`ProbeResult`) are the
contract that lets composite operations like ``enter_recovery`` make
safe decisions when the underlying USB transport flakes mid-sequence
— see ``docs/SPEC_actuators.md`` ("USB disconnect mid-composite").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class WriteOutcome(Enum):
    """Structured result for a single :meth:`RelayDriver.set_channel` call."""

    OK = "ok"  # write completed and was acknowledged (or believed-acked)
    WRITE_FAILED = "write_failed"  # device responded but rejected the command
    DEVICE_GONE = "device_gone"  # USB disconnect / port closed mid-write


class ProbeResult(Enum):
    """Coarse health classification returned by :meth:`RelayDriver.probe`."""

    OK = "ok"
    UNREACHABLE = "unreachable"  # cannot open the transport at all
    AUTH_ERROR = "auth_error"  # placeholder for driver families that need it
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeOutcome:
    """Read-only health probe response.

    Probes are explicitly **side-effect-free** — they MUST NOT toggle
    a channel even on write-only hardware. This avoids drops of held
    pre-power straps mid-flash. See SPEC_actuators.md §"Health probes".
    """

    result: ProbeResult
    detail: Optional[str] = None  # human-readable extra info


@dataclass(frozen=True)
class Transport:
    """Connection parameters passed to :meth:`RelayDriver.open`.

    Only the fields a given driver needs are populated; extras are
    ignored. Captured as a dataclass so future drivers can grow new
    fields without breaking the ABC signature.
    """

    device_path: Optional[str] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    serial_no: Optional[str] = None
    baud: int = 9600
    open_timeout_seconds: float = 2.0


class DriverError(Exception):
    """Raised by drivers for unrecoverable errors (bad config, etc.).

    Transient I/O issues should be reported as :class:`WriteOutcome`
    or :class:`ProbeOutcome` instead, not raised — the runtime relies
    on the structured outcome to decide whether to abort a composite.
    """


class RelayDriver(ABC):
    """Minimal contract every concrete actuator driver implements.

    Lifecycle:
        1. Construct (cheap, no I/O).
        2. :meth:`open` once with a :class:`Transport`.
        3. Any number of :meth:`set_channel` / :meth:`get_channel` /
           :meth:`probe` calls.
        4. :meth:`close` to release the transport.

    Concrete drivers MUST be safe to instantiate without an event loop
    or any I/O so the manager can construct them lazily during config
    loading. All I/O happens inside :meth:`open` and later calls.
    """

    @abstractmethod
    def open(self, transport: Transport) -> None:
        """Acquire the transport. Idempotent if already open."""

    @abstractmethod
    def close(self) -> None:
        """Release the transport. Idempotent on a closed driver."""

    @abstractmethod
    def set_channel(self, index: int, *, closed: bool) -> WriteOutcome:
        """Drive a channel to ``closed`` (True) or ``open`` (False).

        Returns a :class:`WriteOutcome`. Drivers SHOULD NOT raise on
        I/O errors — return ``DEVICE_GONE`` or ``WRITE_FAILED`` instead
        so callers can make composite-safe decisions.
        """

    @abstractmethod
    def get_channel(self, index: int) -> Optional[bool]:
        """Return ``True`` if closed, ``False`` if open, ``None`` if not queryable.

        LCUS-1 returns ``None`` here; Numato can read back actual state.
        """

    @abstractmethod
    def channel_count(self) -> Optional[int]:
        """Return the device's channel count, or ``None`` if not enumerable.

        For non-enumerable drivers (LCUS-1), the manager's provisioning
        ``--channels`` hint is authoritative. For enumerable drivers
        (Numato), this overrides the hint and a mismatch is rejected
        at provisioning time.
        """

    @abstractmethod
    def probe(self) -> ProbeOutcome:
        """Read-only health check. MUST NOT toggle a channel."""
