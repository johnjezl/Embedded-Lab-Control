"""Tests for the relay-driver ABC and the in-memory mock driver."""

import pytest

from labctl.actuators import (
    DriverError,
    ProbeResult,
    Transport,
    WriteOutcome,
)
from labctl.actuators.mock import MockRelayDriver


class TestMockDriverBasics:
    def test_lifecycle(self):
        d = MockRelayDriver(channel_count=4)
        d.open(Transport(device_path="/dev/null"))
        assert d.set_channel(1, closed=True) is WriteOutcome.OK
        assert d.get_channel(1) is True
        d.close()

    def test_set_channel_records_to_log(self):
        d = MockRelayDriver(channel_count=2)
        d.open(Transport())
        d.set_channel(1, closed=True)
        d.set_channel(2, closed=False)
        assert d.write_log == [
            (1, True, WriteOutcome.OK),
            (2, False, WriteOutcome.OK),
        ]

    def test_out_of_range_channel_returns_write_failed(self):
        d = MockRelayDriver(channel_count=1)
        d.open(Transport())
        assert d.set_channel(2, closed=True) is WriteOutcome.WRITE_FAILED

    def test_set_channel_requires_open(self):
        d = MockRelayDriver()
        with pytest.raises(DriverError):
            d.set_channel(1, closed=True)


class TestMockDriverFailureModes:
    def test_next_write_outcome_overrides_one_call(self):
        d = MockRelayDriver()
        d.open(Transport())
        d.next_write_outcome = WriteOutcome.DEVICE_GONE

        assert d.set_channel(1, closed=True) is WriteOutcome.DEVICE_GONE
        # Override only fires once.
        assert d.set_channel(1, closed=True) is WriteOutcome.OK

    def test_device_gone_does_not_persist_state(self):
        """A DEVICE_GONE write must NOT update the cached channel state.

        Composite operations rely on this: after a failed write, the
        next get_channel must reflect "we don't know" rather than the
        commanded value, so abort decisions aren't made on phantom state.
        """
        d = MockRelayDriver()
        d.open(Transport())
        d.next_write_outcome = WriteOutcome.DEVICE_GONE

        d.set_channel(1, closed=True)
        assert d.get_channel(1) is None  # state never persisted

    def test_probe_result_is_configurable(self):
        d = MockRelayDriver()
        d.probe_result = ProbeResult.UNREACHABLE
        outcome = d.probe()
        assert outcome.result is ProbeResult.UNREACHABLE


class TestMockDriverQueryability:
    def test_non_queryable_returns_none(self):
        """LCUS-1-style drivers can't read state back; mock that with queryable=False."""
        d = MockRelayDriver(queryable=False)
        d.open(Transport())
        d.set_channel(1, closed=True)
        assert d.get_channel(1) is None  # not queryable

    def test_queryable_returns_cached_state(self):
        d = MockRelayDriver(queryable=True)
        d.open(Transport())
        d.set_channel(1, closed=True)
        d.set_channel(1, closed=False)
        assert d.get_channel(1) is False


class TestProbeIsSideEffectFree:
    """probe() MUST NOT toggle a channel — see SPEC §"Health probes"."""

    def test_probe_does_not_change_state(self):
        d = MockRelayDriver()
        d.open(Transport())
        d.set_channel(1, closed=True)
        before = dict(d._states)

        d.probe()
        d.probe()

        assert d._states == before
        # Probes must not appear in the write log either.
        assert all(entry[2] is WriteOutcome.OK for entry in d.write_log)
        assert len(d.write_log) == 1
