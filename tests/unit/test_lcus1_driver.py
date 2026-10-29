"""Tests for the LCUS-1 (CH340) USB-relay driver.

Hardware-free: a fake `serial.Serial` is patched into ``sys.modules``
so the driver's lazy import resolves to our recorder/raiser. No
pyserial-level mocking framework needed; the driver only uses a small
subset of the API.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from labctl.actuators.base import (
    DriverError,
    ProbeResult,
    Transport,
    WriteOutcome,
)
from labctl.actuators.lcus1 import (
    LCUS1_FRAME_LEN,
    Lcus1SerialDriver,
    _build_frame,
)

# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------


class TestFrameBuilder:
    def test_channel_1_close(self):
        assert _build_frame(1, closed=True) == bytes([0xA0, 0x01, 0x01, 0xA2])

    def test_channel_1_open(self):
        assert _build_frame(1, closed=False) == bytes([0xA0, 0x01, 0x00, 0xA1])

    def test_channel_2_close(self):
        assert _build_frame(2, closed=True) == bytes([0xA0, 0x02, 0x01, 0xA3])

    def test_checksum_wraps_at_byte(self):
        # channel=255, closed=1 → 0xA0 + 0xFF + 0x01 = 0x1A0; low byte = 0xA0
        frame = _build_frame(0xFF, closed=True)
        assert frame[-1] == 0xA0

    def test_frame_is_four_bytes(self):
        assert len(_build_frame(1, closed=True)) == LCUS1_FRAME_LEN


# ---------------------------------------------------------------------------
# Fake pyserial — minimal subset the driver uses
# ---------------------------------------------------------------------------


class _FakePort:
    """Configurable replacement for ``serial.Serial``."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.writes: list[bytes] = []
        self.flushed = 0
        self.closed = False
        # Control flags settable by tests *after* construction.
        self.write_raises: Exception | None = None
        self.flush_raises: Exception | None = None
        self.write_short_by: int = 0  # report N fewer bytes than written

    def write(self, data: bytes) -> int:
        if self.write_raises is not None:
            raise self.write_raises
        self.writes.append(bytes(data))
        return len(data) - self.write_short_by

    def flush(self) -> None:
        if self.flush_raises is not None:
            raise self.flush_raises
        self.flushed += 1

    def close(self) -> None:
        self.closed = True


class _FakeSerialModule(types.ModuleType):
    """Stub for ``serial``: holds the latest constructed port for tests."""

    PARITY_NONE = "N"

    class SerialException(Exception):
        pass

    def __init__(self, name="serial"):
        super().__init__(name)
        self.last_port: _FakePort | None = None
        self.constructor_raises: Exception | None = None

    def Serial(self, *args, **kwargs):  # noqa: N802 — match real API
        if self.constructor_raises is not None:
            raise self.constructor_raises
        port = _FakePort(*args, **kwargs)
        self.last_port = port
        return port


@pytest.fixture
def fake_serial(monkeypatch):
    fake = _FakeSerialModule()
    monkeypatch.setitem(sys.modules, "serial", fake)
    yield fake


# ---------------------------------------------------------------------------
# Driver behaviour
# ---------------------------------------------------------------------------


class TestOpenClose:
    def test_open_passes_baud_and_path(self, fake_serial):
        d = Lcus1SerialDriver(expected_channel_count=1)
        d.open(Transport(device_path="/dev/ttyUSB0", baud=9600))

        assert fake_serial.last_port is not None
        kw = fake_serial.last_port.kwargs
        assert kw["port"] == "/dev/ttyUSB0"
        assert kw["baudrate"] == 9600

    def test_open_idempotent(self, fake_serial):
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        first = fake_serial.last_port
        d.open(Transport(device_path="/dev/ttyUSB0"))
        # Second open must not construct a new Serial.
        assert fake_serial.last_port is first

    def test_open_without_device_path_raises(self, fake_serial):
        d = Lcus1SerialDriver()
        with pytest.raises(DriverError, match="device_path"):
            d.open(Transport())

    def test_open_translates_serial_error_to_driver_error(self, fake_serial):
        fake_serial.constructor_raises = fake_serial.SerialException("nope")
        d = Lcus1SerialDriver()
        with pytest.raises(DriverError, match="failed to open"):
            d.open(Transport(device_path="/dev/ttyUSB-bad"))

    def test_permission_denied_carries_the_hint(self, fake_serial, monkeypatch):
        """pyserial reports EACCES as SerialException(errno=13)."""
        import errno

        from labctl.actuators import lcus1

        monkeypatch.setattr(lcus1, "device_permission_hint", lambda p: "HINT")
        err = fake_serial.SerialException(errno.EACCES, "could not open port")
        err.errno = errno.EACCES
        fake_serial.constructor_raises = err
        with pytest.raises(DriverError) as exc:
            Lcus1SerialDriver().open(Transport(device_path="/dev/x"))
        assert str(exc.value) == "permission denied opening /dev/x: HINT"

    def test_other_open_errors_unchanged(self, fake_serial):
        import errno

        err = fake_serial.SerialException(errno.ENOENT, "no such device")
        err.errno = errno.ENOENT
        fake_serial.constructor_raises = err
        with pytest.raises(DriverError, match="failed to open /dev/x: "):
            Lcus1SerialDriver().open(Transport(device_path="/dev/x"))

    def test_close_idempotent(self, fake_serial):
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        d.close()
        d.close()  # must not raise


class TestSetChannel:
    def _open(self, fake_serial, **kw):
        d = Lcus1SerialDriver(**kw)
        d.open(Transport(device_path="/dev/ttyUSB0"))
        return d, fake_serial.last_port

    def test_writes_correct_frame(self, fake_serial):
        d, port = self._open(fake_serial)
        outcome = d.set_channel(1, closed=True)
        assert outcome is WriteOutcome.OK
        assert port.writes == [bytes([0xA0, 0x01, 0x01, 0xA2])]
        assert port.flushed == 1

    def test_disconnect_returns_device_gone(self, fake_serial):
        d, port = self._open(fake_serial)
        port.write_raises = OSError("usb gone")
        outcome = d.set_channel(1, closed=True)
        assert outcome is WriteOutcome.DEVICE_GONE

    def test_short_write_returns_write_failed(self, fake_serial):
        d, port = self._open(fake_serial)
        port.write_short_by = 2
        outcome = d.set_channel(1, closed=False)
        assert outcome is WriteOutcome.WRITE_FAILED

    def test_flush_failure_treated_as_device_gone(self, fake_serial):
        d, port = self._open(fake_serial)
        port.flush_raises = OSError("flush failed")
        outcome = d.set_channel(1, closed=True)
        assert outcome is WriteOutcome.DEVICE_GONE

    def test_index_out_of_range_returns_write_failed(self, fake_serial):
        d, port = self._open(fake_serial, expected_channel_count=2)
        # 0 is invalid (1-based)
        assert d.set_channel(0, closed=True) is WriteOutcome.WRITE_FAILED
        # > expected
        assert d.set_channel(3, closed=True) is WriteOutcome.WRITE_FAILED
        assert port.writes == []

    def test_set_before_open_raises(self):
        d = Lcus1SerialDriver()
        with pytest.raises(DriverError, match="not open"):
            d.set_channel(1, closed=True)


class TestQueryability:
    def test_get_channel_always_none(self, fake_serial):
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        d.set_channel(1, closed=True)
        assert d.get_channel(1) is None

    def test_channel_count_returns_hint(self):
        assert Lcus1SerialDriver(expected_channel_count=4).channel_count() == 4
        assert Lcus1SerialDriver().channel_count() is None


class TestProbeIsSideEffectFree:
    def test_probe_when_open_returns_ok_without_writing(self, fake_serial):
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        port = fake_serial.last_port

        outcome = d.probe()

        assert outcome.result is ProbeResult.OK
        # Probe MUST NOT toggle a channel even on write-only hardware.
        assert port.writes == []

    def test_probe_after_close_reopens_transiently(self, fake_serial):
        """Probe after close opens + closes a fresh port to verify reachability."""
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        d.close()

        outcome = d.probe()

        assert outcome.result is ProbeResult.OK
        # The transient probe port should have been closed too.
        assert fake_serial.last_port is not None
        assert fake_serial.last_port.closed is True
        # And no writes happened.
        assert fake_serial.last_port.writes == []

    def test_probe_unreachable(self, fake_serial):
        d = Lcus1SerialDriver()
        d.open(Transport(device_path="/dev/ttyUSB0"))
        d.close()

        fake_serial.constructor_raises = fake_serial.SerialException("nope")
        outcome = d.probe()
        assert outcome.result is ProbeResult.UNREACHABLE
        assert outcome.detail and "nope" in outcome.detail

    def test_probe_before_first_open_returns_unknown(self, fake_serial):
        d = Lcus1SerialDriver()
        outcome = d.probe()
        assert outcome.result is ProbeResult.UNKNOWN


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_get_driver_returns_lcus1(self):
        from labctl.actuators import get_driver
        from labctl.core.models import DriverName

        d = get_driver(DriverName.LCUS1_SERIAL, expected_channel_count=2)
        assert isinstance(d, Lcus1SerialDriver)
        assert d.channel_count() == 2

    def test_get_driver_unimplemented(self):
        from labctl.actuators import get_driver
        from labctl.core.models import DriverName

        with pytest.raises(NotImplementedError):
            get_driver(DriverName.NUMATO_ACM)


class TestDevicePermissionHint:
    """The advice depends on the device's group and mode and on whether
    the account (or this process) is already in that group."""

    @pytest.fixture
    def device(self, tmp_path, monkeypatch):
        import grp
        import pwd
        from types import SimpleNamespace

        path = tmp_path / "relay"
        path.write_text("")
        path.chmod(0o660)
        gid = os.stat(path).st_gid
        members: list[str] = []
        monkeypatch.setattr(
            pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="alice")
        )
        monkeypatch.setattr(
            grp,
            "getgrgid",
            lambda g: SimpleNamespace(gr_name="dialout", gr_mem=members),
        )
        # By default this process is not in the device's group.
        monkeypatch.setattr(os, "getgroups", lambda: [])
        monkeypatch.setattr(os, "getegid", lambda: gid + 1)
        return SimpleNamespace(path=path, gid=gid, members=members)

    def _hint(self, path):
        from labctl.actuators.base import device_permission_hint

        return device_permission_hint(str(path))

    def test_not_in_group(self, device):
        hint = self._hint(device.path)
        assert "alice needs to be in the 'dialout' group" in hint
        assert "sudo usermod -aG dialout alice" in hint
        assert "newgrp dialout" in hint
        assert "restart any service or MCP client that runs labctl" in hint

    def test_account_in_group_but_process_is_not(self, device):
        device.members.append("alice")
        hint = self._hint(device.path)
        assert "alice is in the 'dialout' group" in hint
        assert "started before it was added; it takes effect" in hint
        assert "usermod" not in hint

    def test_process_already_in_group(self, device, monkeypatch):
        monkeypatch.setattr(os, "getgroups", lambda: [device.gid])
        hint = self._hint(device.path)
        assert "already in the 'dialout' group" in hint
        assert "something else denies access" in hint
        assert "usermod" not in hint

    def test_group_without_read_write_gets_generic_advice(self, device):
        device.path.chmod(0o640)
        hint = self._hint(device.path)
        assert "usermod" not in hint and "ls -lL" in hint

    def test_root_group_never_recommended(self, device, monkeypatch):
        real_stat = os.stat

        def stat_root_group(p, *a, **kw):
            st = real_stat(p, *a, **kw)
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, 0)
                + tuple(st)[6:]
            )

        monkeypatch.setattr(os, "stat", stat_root_group)
        hint = self._hint(device.path)
        assert "usermod" not in hint and "ls -lL" in hint

    def test_missing_device_gets_generic_advice(self, device, tmp_path):
        hint = self._hint(tmp_path / "gone")
        assert hint.startswith("check that alice may read and write ")

    def test_no_account_name_never_suggests_usermod(self, device, monkeypatch):
        import pwd

        def no_entry(uid):
            raise KeyError(uid)

        monkeypatch.setattr(pwd, "getpwuid", no_entry)
        hint = self._hint(device.path)
        assert "usermod" not in hint
        assert hint.startswith(f"check that uid {os.geteuid()} may read and write")
