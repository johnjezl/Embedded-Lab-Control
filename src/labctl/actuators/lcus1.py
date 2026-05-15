"""LCUS-1 (CH340-based USB relay) driver.

Hardware notes
==============
LCUS-1 boards are the cheap CH340-clone single-channel USB relays
(also branded "USR-IO" or unbranded). They speak a 4-byte fixed-frame
protocol at 9600/8N1; there is no acknowledgement and no state
read-back. Relay state must be tracked in software.

Frame layout (no spaces in the wire bytes):

    AA  CC  SS  KK
    │   │   │   └─ checksum: low byte of (AA + CC + SS)
    │   │   └──── state: 0x00 = open, 0x01 = close
    │   └──────── channel index, 1-based (typically 0x01)
    └──────────── start byte 0xA0

Examples:

    A0 01 01 A2   →   channel 1 close
    A0 01 00 A1   →   channel 1 open
    A0 02 01 A3   →   channel 2 close (multi-channel clones)

Probe semantics
---------------
We deliberately do NOT toggle a channel during ``probe`` — see
``docs/SPEC_actuators.md`` §"Health probes". A held ``recovery_mode``
strap mid-flash must survive a probe cycle. Probe = "open the serial
port, verify it opens, close" — that's it.

Concurrency
-----------
The driver itself is not thread-safe across multiple ``set_channel``
calls; the manager-level per-channel lock (added in Phase 4) is what
serializes writes. ``open`` and ``close`` are idempotent.
"""

from __future__ import annotations

import logging
from typing import Optional

from labctl.actuators.base import (
    DriverError,
    ProbeOutcome,
    ProbeResult,
    RelayDriver,
    Transport,
    WriteOutcome,
)

logger = logging.getLogger(__name__)


# Frame layout constants. Documented inline in the module docstring above.
LCUS1_START_BYTE = 0xA0
LCUS1_STATE_CLOSED = 0x01
LCUS1_STATE_OPEN = 0x00
LCUS1_FRAME_LEN = 4


def _build_frame(channel_index: int, *, closed: bool) -> bytes:
    """Build a 4-byte LCUS-1 command frame."""
    state = LCUS1_STATE_CLOSED if closed else LCUS1_STATE_OPEN
    checksum = (LCUS1_START_BYTE + channel_index + state) & 0xFF
    return bytes([LCUS1_START_BYTE, channel_index, state, checksum])


class Lcus1SerialDriver(RelayDriver):
    """LCUS-1 / generic CH340 single-channel USB relay driver."""

    def __init__(self, *, expected_channel_count: Optional[int] = None):
        """Construct the driver. Hardware I/O happens in :meth:`open`.

        Args:
            expected_channel_count: hint from provisioning. LCUS-1 has
                no enumeration protocol, so this value is what
                :meth:`channel_count` returns. The manager treats this
                as authoritative for non-enumerable drivers.
        """
        self._port = None  # type: Optional[object]  # serial.Serial
        self._transport: Optional[Transport] = None
        self._expected_channels = expected_channel_count

    # -- lifecycle ---------------------------------------------------------

    def open(self, transport: Transport) -> None:
        """Open the serial port. Idempotent if already open."""
        if self._port is not None:
            return
        try:
            import serial  # type: ignore
        except ImportError as e:
            raise DriverError(
                "pyserial is required for the lcus1_serial driver"
            ) from e

        if not transport.device_path:
            raise DriverError(
                "lcus1_serial driver requires transport.device_path"
            )

        try:
            self._port = serial.Serial(
                port=transport.device_path,
                baudrate=transport.baud or 9600,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=transport.open_timeout_seconds,
                write_timeout=transport.open_timeout_seconds,
            )
        except (OSError, serial.SerialException) as e:
            self._port = None
            raise DriverError(
                f"failed to open {transport.device_path}: {e}"
            ) from e
        self._transport = transport
        logger.debug("LCUS-1 opened %s", transport.device_path)

    def close(self) -> None:
        """Close the serial port. Idempotent on a closed driver."""
        if self._port is None:
            return
        try:
            self._port.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("LCUS-1 close raised %s; ignoring", e)
        finally:
            self._port = None

    def _require_open(self) -> None:
        if self._port is None:
            raise DriverError("driver not open; call open(transport) first")

    # -- I/O ---------------------------------------------------------------

    def set_channel(self, index: int, *, closed: bool) -> WriteOutcome:
        """Drive a channel; return a structured outcome.

        Returns ``DEVICE_GONE`` for transport-level failures (USB
        disconnect, port closed) and ``WRITE_FAILED`` for short writes
        or other recoverable error paths.
        """
        self._require_open()
        if index < 1:
            return WriteOutcome.WRITE_FAILED
        if (
            self._expected_channels is not None
            and index > self._expected_channels
        ):
            return WriteOutcome.WRITE_FAILED

        frame = _build_frame(index, closed=closed)
        try:
            written = self._port.write(frame)
            self._port.flush()
        except Exception as e:  # noqa: BLE001 — pyserial raises a zoo of these
            # Any transport-level failure is reported as DEVICE_GONE so
            # composite operations abort safely instead of retrying onto
            # a vanished device.
            logger.warning(
                "LCUS-1 write to channel %d failed: %s: %s",
                index,
                type(e).__name__,
                e,
            )
            return WriteOutcome.DEVICE_GONE

        if written != LCUS1_FRAME_LEN:
            logger.warning(
                "LCUS-1 short write to channel %d: %d/%d bytes",
                index,
                written,
                LCUS1_FRAME_LEN,
            )
            return WriteOutcome.WRITE_FAILED
        return WriteOutcome.OK

    def get_channel(self, index: int) -> Optional[bool]:
        """LCUS-1 has no read-back; always returns None."""
        return None

    def channel_count(self) -> Optional[int]:
        """No enumeration protocol — return the provisioning hint."""
        return self._expected_channels

    # -- probe -------------------------------------------------------------

    def probe(self) -> ProbeOutcome:
        """Read-only probe: try opening the port if not already open.

        MUST NOT toggle a channel even on write-only hardware — a held
        pre_power strap during a flash must survive probes.
        """
        if self._port is not None:
            return ProbeOutcome(result=ProbeResult.OK)

        # Opening transiently to verify reachability without writing.
        if self._transport is None:
            return ProbeOutcome(
                result=ProbeResult.UNKNOWN,
                detail="probe before first open() — no transport hint",
            )

        try:
            import serial  # type: ignore
        except ImportError:
            return ProbeOutcome(
                result=ProbeResult.UNKNOWN, detail="pyserial not installed"
            )

        try:
            test = serial.Serial(
                port=self._transport.device_path,
                baudrate=self._transport.baud or 9600,
                timeout=self._transport.open_timeout_seconds,
            )
            test.close()
        except (OSError, serial.SerialException) as e:
            return ProbeOutcome(
                result=ProbeResult.UNREACHABLE, detail=str(e)
            )
        return ProbeOutcome(result=ProbeResult.OK)
