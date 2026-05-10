"""In-memory mock driver for tests.

Programmable failure modes let tests exercise the
:class:`~labctl.actuators.base.WriteOutcome` paths (composite abort
on ``DEVICE_GONE``, etc.) without requiring real hardware.
"""

from __future__ import annotations

from typing import Optional

from labctl.actuators.base import (
    DriverError,
    ProbeOutcome,
    ProbeResult,
    RelayDriver,
    Transport,
    WriteOutcome,
)


class MockRelayDriver(RelayDriver):
    """Deterministic, in-memory relay used by tests and dry-runs.

    State for each channel is kept in a dict; reads return the cached
    value (so :meth:`get_channel` returns the truth post-write).

    Failure-mode toggles:
        * ``next_write_outcome`` — if set, the next ``set_channel`` call
          returns this outcome and clears the override. Useful for
          driving composite-abort paths.
        * ``probe_result`` — what :meth:`probe` returns. Default OK.
        * ``queryable`` — when False, :meth:`get_channel` returns None
          (LCUS-1-like behavior).
    """

    def __init__(self, *, channel_count: int = 1, queryable: bool = True):
        self._configured_count = channel_count
        self._queryable = queryable
        self._states: dict[int, bool] = {}
        self._open = False
        self.next_write_outcome: Optional[WriteOutcome] = None
        self.probe_result: ProbeResult = ProbeResult.OK
        self.write_log: list[tuple[int, bool, WriteOutcome]] = []

    def open(self, transport: Transport) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _require_open(self) -> None:
        if not self._open:
            raise DriverError("driver not open")

    def set_channel(self, index: int, *, closed: bool) -> WriteOutcome:
        self._require_open()
        if index < 1 or index > self._configured_count:
            return WriteOutcome.WRITE_FAILED
        if self.next_write_outcome is not None:
            outcome = self.next_write_outcome
            self.next_write_outcome = None
            self.write_log.append((index, closed, outcome))
            if outcome is WriteOutcome.OK:
                self._states[index] = closed
            return outcome
        self._states[index] = closed
        self.write_log.append((index, closed, WriteOutcome.OK))
        return WriteOutcome.OK

    def get_channel(self, index: int) -> Optional[bool]:
        self._require_open()
        if not self._queryable:
            return None
        return self._states.get(index)

    def channel_count(self) -> Optional[int]:
        return self._configured_count

    def probe(self) -> ProbeOutcome:
        return ProbeOutcome(result=self.probe_result)
