"""Unit tests for SDWire controller."""

import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from labctl.sdwire.controller import SDWireController

# Every test here runs against the sys.modules stub (tests/conftest.py), so the
# suite passes without the optional sdwire package (which needs Python >= 3.12).
pytestmark = pytest.mark.usefixtures("sdwire_stub")


class TestSDWireController:
    """Tests for SDWireController."""

    def test_init(self):
        """Test controller initialization."""
        ctrl = SDWireController("bdgrd_sdwirec_522")
        assert ctrl.serial_number == "bdgrd_sdwirec_522"

    def test_get_device_not_found(self):
        """Test _get_device raises when device not connected."""
        ctrl = SDWireController("nonexistent_serial")

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                with pytest.raises(RuntimeError, match="not found"):
                    ctrl._get_device()

    def test_get_device_found(self):
        """Test _get_device returns matching device."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"

        with patch(
            "sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]
        ):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                result = ctrl._get_device()

        assert result is mock_dev

    def test_get_device_skips_non_matching(self):
        """Test _get_device skips devices with wrong serial."""
        ctrl = SDWireController("target_serial")

        other_dev = MagicMock()
        other_dev.serial_string = "other_serial"

        target_dev = MagicMock()
        target_dev.serial_string = "target_serial"

        with patch(
            "sdwire.backend.detect.get_sdwirec_devices", return_value=[other_dev]
        ):
            with patch(
                "sdwire.backend.detect.get_sdwire_devices", return_value=[target_dev]
            ):
                result = ctrl._get_device()

        assert result is target_dev

    def test_switch_to_dut(self):
        """Test switch_to_dut calls device.switch_dut()."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            ctrl.switch_to_dut()

        mock_dev.switch_dut.assert_called_once()

    def test_switch_to_host(self):
        """Test switch_to_host calls device.switch_ts()."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            ctrl.switch_to_host()

        mock_dev.switch_ts.assert_called_once()

    def test_get_block_device(self):
        """Test get_block_device returns block_dev attribute."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"
        mock_dev.block_dev = "/dev/sdb"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            with patch(
                "labctl.sdwire.controller._block_device_has_media", return_value=True
            ):
                result = ctrl.get_block_device()

        assert result == "/dev/sdb"

    def test_get_block_device_none(self):
        """Test get_block_device returns None when not available."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock(spec=[])  # No block_dev attribute

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            result = ctrl.get_block_device()

        assert result is None

    def test_get_block_device_skips_zero_size(self):
        """Test get_block_device returns None for stale devices with no media."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"
        mock_dev.block_dev = "/dev/sdc"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            with patch(
                "labctl.sdwire.controller._block_device_has_media", return_value=False
            ):
                result = ctrl.get_block_device()

        assert result is None

    def test_get_block_device_returns_valid(self):
        """Test get_block_device returns library result when it has media."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"
        mock_dev.block_dev = "/dev/sdb"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            with patch(
                "labctl.sdwire.controller._block_device_has_media", return_value=True
            ):
                result = ctrl.get_block_device()

        assert result == "/dev/sdb"

    def test_get_block_device_device_not_found(self):
        """Test get_block_device returns None when device not connected."""
        ctrl = SDWireController("missing_serial")

        with patch.object(ctrl, "_get_device", side_effect=RuntimeError("not found")):
            result = ctrl.get_block_device()

        assert result is None

    def test_switch_to_dut_device_not_connected(self):
        """Test switch_to_dut raises when device not connected."""
        ctrl = SDWireController("missing_serial")

        with patch.object(ctrl, "_get_device", side_effect=RuntimeError("not found")):
            with pytest.raises(RuntimeError, match="not found"):
                ctrl.switch_to_dut()

    def test_switch_to_host_device_not_connected(self):
        """Test switch_to_host raises when device not connected."""
        ctrl = SDWireController("missing_serial")

        with patch.object(ctrl, "_get_device", side_effect=RuntimeError("not found")):
            with pytest.raises(RuntimeError, match="not found"):
                ctrl.switch_to_host()

    def test_flash_image_no_block_device(self):
        """Test flash_image raises when no block device found."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value=None):
            with pytest.raises(RuntimeError, match="Cannot determine block device"):
                ctrl.flash_image("/path/to/image.img")

    def test_flash_image_success(self):
        """Test flash_image runs dd and sync, returns result dict."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                with patch("labctl.sdwire.controller._validate_block_device"):
                    with patch("labctl.sdwire.controller._validate_image_file"):
                        with patch("os.path.getsize", return_value=1024000):
                            result = ctrl.flash_image("/path/to/image.img")

        assert isinstance(result, dict)
        assert result["bytes_written"] == 1024000
        assert result["elapsed_seconds"] >= 0
        assert result["block_device"] == "/dev/sdb"

    def test_flash_image_dd_fails(self):
        """Test flash_image raises on dd failure."""
        import subprocess

        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller._validate_block_device"):
                with patch("labctl.sdwire.controller._validate_image_file"):
                    with patch(
                        "labctl.sdwire.controller.subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, "dd"),
                    ):
                        with pytest.raises(RuntimeError, match="Flash failed"):
                            ctrl.flash_image("/path/to/image.img")


class TestUpdateFiles:
    """Tests for SDWireController.update_files."""

    def test_update_files_no_block_device(self):
        """Test update_files raises when no block device found."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value=None):
            with pytest.raises(RuntimeError, match="Cannot determine block device"):
                ctrl.update_files(1, [("src.bin", "dest.bin")])

    def test_update_files_mount_fails(self):
        """Test update_files raises when mount fails."""
        import subprocess

        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.CalledProcessError(
                    1, "mount", stderr="mount: permission denied"
                )
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with pytest.raises(RuntimeError, match="Failed to mount"):
                            ctrl.update_files(1, [("src.bin", "dest.bin")])

    def test_update_files_success(self):
        """Test update_files mounts, copies, unmounts."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run") as mock_run:
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("shutil.copy2") as mock_copy:
                        with patch("os.rmdir"):
                            with patch("os.path.exists", return_value=True):
                                result = ctrl.update_files(
                                    1,
                                    [("local.bin", "kernel.img")],
                                )

        assert result == {"copied": ["kernel.img"], "renamed": [], "deleted": []}
        # Verify mount was called with correct partition
        mount_call = mock_run.call_args_list[0]
        mount_cmd = mount_call[0][0]
        assert mount_cmd[0:2] == ["sudo", "mount"]
        assert "-o" in mount_cmd
        assert "/dev/sdb1" in mount_cmd
        assert "/tmp/labctl-test" in mount_cmd
        # Verify copy
        mock_copy.assert_called_once()
        # Verify unmount
        umount_call = mock_run.call_args_list[1]
        assert umount_call[0][0] == ["sudo", "umount", "/tmp/labctl-test"]

    def test_update_files_multiple(self):
        """Test update_files copies multiple files."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("shutil.copy2"):
                        with patch("os.rmdir"):
                            with patch("os.path.exists", return_value=True):
                                result = ctrl.update_files(
                                    1,
                                    [
                                        ("a.bin", "kernel.img"),
                                        ("b.txt", "config.txt"),
                                    ],
                                )

        assert result["copied"] == ["kernel.img", "config.txt"]

    def test_update_files_rename(self):
        """Test update_files renames files."""
        ctrl = SDWireController("test_serial")

        def exists_side_effect(path):
            # Source exists, destination does not
            return path != "/tmp/labctl-test/new.bin"

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", side_effect=exists_side_effect):
                            with patch("os.rename") as mock_rename:
                                result = ctrl.update_files(
                                    1,
                                    [],
                                    renames=[("old.bin", "new.bin")],
                                )

        assert result["renamed"] == ["old.bin -> new.bin"]
        mock_rename.assert_called_once_with(
            "/tmp/labctl-test/old.bin", "/tmp/labctl-test/new.bin"
        )

    def test_update_files_rename_source_missing(self):
        """Test rename raises when source file doesn't exist."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", return_value=False):
                            with pytest.raises(RuntimeError, match="not found"):
                                ctrl.update_files(
                                    1,
                                    [],
                                    renames=[("missing.bin", "new.bin")],
                                )

    def test_update_files_rename_dest_exists(self):
        """Test rename raises when destination already exists."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", return_value=True):
                            with pytest.raises(RuntimeError, match="already exists"):
                                ctrl.update_files(
                                    1,
                                    [],
                                    renames=[("old.bin", "existing.bin")],
                                )

    def test_update_files_delete(self):
        """Test update_files deletes files."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", return_value=True):
                            with patch("os.path.isdir", return_value=False):
                                with patch("os.remove") as mock_remove:
                                    result = ctrl.update_files(
                                        1,
                                        [],
                                        deletes=["stale.txt"],
                                    )

        assert result["deleted"] == ["stale.txt"]
        mock_remove.assert_called_once_with("/tmp/labctl-test/stale.txt")

    def test_update_files_delete_missing(self):
        """Test delete raises when file doesn't exist."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", return_value=False):
                            with pytest.raises(RuntimeError, match="not found"):
                                ctrl.update_files(
                                    1,
                                    [],
                                    deletes=["missing.txt"],
                                )

    def test_update_files_delete_directory_rejected(self):
        """Test delete raises when target is a directory."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with patch("os.path.exists", return_value=True):
                            with patch("os.path.isdir", return_value=True):
                                with pytest.raises(
                                    RuntimeError, match="is a directory"
                                ):
                                    ctrl.update_files(
                                        1,
                                        [],
                                        deletes=["somedir"],
                                    )

    def test_update_files_combined_operations(self):
        """Test copy, rename, and delete in one call."""
        ctrl = SDWireController("test_serial")

        def exists_side_effect(path):
            # Rename destination doesn't exist yet
            return path != "/tmp/labctl-test/a.bin.bak"

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("shutil.copy2"):
                        with patch("os.rmdir"):
                            with patch(
                                "os.path.exists", side_effect=exists_side_effect
                            ):
                                with patch("os.path.isdir", return_value=False):
                                    with patch("os.rename"):
                                        with patch("os.remove"):
                                            result = ctrl.update_files(
                                                1,
                                                [("src.bin", "kernel.img")],
                                                renames=[("a.bin", "a.bin.bak")],
                                                deletes=["old.txt"],
                                            )

        assert result["copied"] == ["kernel.img"]
        assert result["renamed"] == ["a.bin -> a.bin.bak"]
        assert result["deleted"] == ["old.txt"]

    def test_update_files_unmounts_on_copy_error(self):
        """Test that unmount runs even if copy fails."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run") as mock_run:
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("shutil.copy2", side_effect=OSError("disk full")):
                        with patch("os.rmdir"):
                            with patch("os.path.exists", return_value=True):
                                with pytest.raises(OSError, match="disk full"):
                                    ctrl.update_files(1, [("a.bin", "b.bin")])

        # Unmount should still have been called
        umount_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "umount"]
        assert len(umount_calls) == 1

    def test_update_files_path_traversal_copy(self):
        """Test that path traversal in copy dest is rejected."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with pytest.raises(RuntimeError, match="Path traversal"):
                            ctrl.update_files(
                                1,
                                [("local.bin", "../../etc/passwd")],
                            )

    def test_update_files_path_traversal_rename(self):
        """Test that path traversal in rename is rejected."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with pytest.raises(RuntimeError, match="Path traversal"):
                            ctrl.update_files(
                                1,
                                [],
                                renames=[("ok.bin", "../../../etc/shadow")],
                            )

    def test_update_files_path_traversal_delete(self):
        """Test that path traversal in delete is rejected."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run"):
                with patch("tempfile.mkdtemp", return_value="/tmp/labctl-test"):
                    with patch("os.rmdir"):
                        with pytest.raises(RuntimeError, match="Path traversal"):
                            ctrl.update_files(
                                1,
                                [],
                                deletes=["../../../etc/passwd"],
                            )


class TestCardOnHost:
    """card_on_host probes the reader instead of trusting the cached size.

    The cached /sys size lags a mux switch in both directions; a one-sector
    `sudo dd` read forces the kernel to re-check for media.
    """

    @staticmethod
    def _run(returncode=0, stderr=""):
        result = MagicMock()
        result.returncode = returncode
        result.stderr = stderr
        return result

    def _check(self, *, node="/dev/sdx", run=None, size_has_media=False):
        ctrl = SDWireController("s")
        run_patch = (
            patch("subprocess.run", side_effect=run)
            if isinstance(run, BaseException)
            else patch("subprocess.run", return_value=run)
        )
        with (
            patch.object(ctrl, "_block_node", return_value=node),
            patch(
                "labctl.sdwire.controller._block_device_has_media",
                return_value=size_has_media,
            ),
            run_patch as mock_run,
        ):
            return ctrl.card_on_host(), mock_run

    def test_no_device_node_means_dut(self):
        on_host, run = self._check(node=None)
        assert on_host is False
        run.assert_not_called()

    def test_readable_media_means_host(self):
        on_host, run = self._check(run=self._run(0))
        assert on_host is True
        cmd = run.call_args.args[0]
        # Read-only probe: one sector to /dev/null, non-interactive sudo.
        assert cmd[:3] == ["sudo", "-n", "dd"]
        assert "if=/dev/sdx" in cmd and "of=/dev/null" in cmd
        assert "count=1" in cmd

    def test_probe_runs_in_c_locale(self):
        """dd's message is matched in English, so force LC_ALL=C."""
        _, run = self._check(run=self._run(0))
        assert run.call_args.kwargs["env"]["LC_ALL"] == "C"

    def test_just_switched_to_host_size_still_zero(self):
        """Review #2 finding 1: size not updated yet, card already readable."""
        on_host, _ = self._check(run=self._run(0), size_has_media=False)
        assert on_host is True

    def test_no_medium_with_stale_size_means_dut(self):
        """Review #1 finding: size still > 0 but the card is on the DUT."""
        stderr = "dd: failed to open '/dev/sdx': No medium found\n"
        on_host, _ = self._check(run=self._run(1, stderr), size_has_media=True)
        assert on_host is False

    @pytest.mark.parametrize("size_has_media", [True, False])
    def test_inconclusive_probe_falls_back_to_size(self, size_has_media):
        """e.g. no sudo rights: can't verify, use the cached size."""
        stderr = "sudo: a password is required\n"
        on_host, _ = self._check(
            run=self._run(1, stderr), size_has_media=size_has_media
        )
        assert on_host is size_has_media

    @pytest.mark.parametrize(
        "exc",
        [FileNotFoundError("sudo"), subprocess.TimeoutExpired("dd", 10)],
    )
    def test_probe_unavailable_falls_back_to_size(self, exc):
        on_host, _ = self._check(run=exc, size_has_media=True)
        assert on_host is True


class TestWaitForHost:
    def test_returns_device_once_card_readable(self):
        ctrl = SDWireController("s")
        with (
            patch.object(ctrl, "card_on_host", side_effect=[False, False, True]),
            patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
            patch("time.sleep") as sleep,
        ):
            assert ctrl.wait_for_host(timeout=10, interval=0.5) == "/dev/sdx"
        assert sleep.call_count == 2

    def test_times_out(self):
        ctrl = SDWireController("s")
        clock = iter([0.0, 0.0, 5.0, 11.0])
        with (
            patch.object(ctrl, "card_on_host", return_value=False),
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("time.sleep"),
        ):
            assert ctrl.wait_for_host(timeout=10) is None


class TestReadOnlyMountNeverReplaysJournal:
    """`-o ro` alone replays a dirty ext4/XFS/F2FS/btrfs journal (a write)."""

    @staticmethod
    def _fake_run(*, lsblk="", head=None, head_rc=0, mount_fails=False, log=None):
        """subprocess.run stand-in: lsblk, `sudo dd` signature read, mount."""

        def run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "lsblk":
                if isinstance(lsblk, BaseException):
                    raise lsblk
                result.stdout = f"{lsblk}\n"
            elif cmd[:3] == ["sudo", "-n", "dd"]:
                if isinstance(head, BaseException):
                    raise head
                result.returncode = head_rc
                result.stdout = head if head is not None else b""
            elif cmd[:2] == ["sudo", "mount"]:
                if log is not None:
                    log.append(cmd[cmd.index("-o") + 1])
                if mount_fails:
                    raise subprocess.CalledProcessError(32, cmd, stderr="nope")
            return result

        return run

    @staticmethod
    def _image(fstype):
        """A fake partition head carrying `fstype`'s signature."""
        from labctl.sdwire.controller import _FS_PROBE_BYTES, _REPLAYING_FS_MAGIC

        data = bytearray(_FS_PROBE_BYTES)
        offset, magic = _REPLAYING_FS_MAGIC[fstype]
        data[offset : offset + len(magic)] = magic
        return bytes(data)

    @pytest.mark.parametrize(
        "fstype,expected",
        [
            ("ext4", [["noload"]]),
            ("ext3", [["noload"]]),
            ("xfs", [["norecovery"]]),
            ("f2fs", [["norecovery"]]),
            ("btrfs", [["rescue=nologreplay"], ["nologreplay"]]),
            ("vfat", [[]]),
        ],
    )
    def test_options_by_lsblk_fstype(self, fstype, expected):
        from labctl.sdwire.controller import _ro_no_replay

        with patch("subprocess.run", side_effect=self._fake_run(lsblk=fstype)):
            assert _ro_no_replay("/dev/sdx2") == expected

    @pytest.mark.parametrize(
        "magic_fs,expected",
        [
            ("ext4", [["noload"]]),
            ("xfs", [["norecovery"]]),
            ("f2fs", [["norecovery"]]),
            ("btrfs", [["rescue=nologreplay"], ["nologreplay"]]),
        ],
    )
    def test_udev_not_ready_signature_decides(self, magic_fs, expected):
        """Review #3 finding: lsblk empty right after a switch."""
        from labctl.sdwire.controller import _ro_no_replay

        run = self._fake_run(lsblk="", head=self._image(magic_fs))
        with patch("subprocess.run", side_effect=run):
            assert _ro_no_replay("/dev/sdx2") == expected

    def test_no_signature_means_plain_ro_is_safe(self):
        """e.g. vfat: no replaying signature, so plain ro."""
        from labctl.sdwire.controller import _FS_PROBE_BYTES, _ro_no_replay

        run = self._fake_run(lsblk="", head=bytes(_FS_PROBE_BYTES))
        with patch("subprocess.run", side_effect=run):
            assert _ro_no_replay("/dev/sdx1") == [[]]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"head": b"", "head_rc": 1},  # dd failed
            {"head": FileNotFoundError("sudo")},  # no sudo
            {"head": subprocess.TimeoutExpired("dd", 10)},
        ],
    )
    def test_type_undeterminable_never_plain_ro(self, kwargs):
        """Unknown type: noload only, no fallback to a replaying plain ro."""
        from labctl.sdwire.controller import _ro_no_replay

        run = self._fake_run(lsblk=FileNotFoundError("lsblk"), **kwargs)
        with patch("subprocess.run", side_effect=run):
            assert _ro_no_replay("/dev/sdx2") == [["noload"]]

    def test_signature_offsets(self):
        """Pin the on-disk magic locations against the format specs."""
        from labctl.sdwire.controller import _REPLAYING_FS_MAGIC

        assert _REPLAYING_FS_MAGIC["ext4"] == (1080, (0xEF53).to_bytes(2, "little"))
        assert _REPLAYING_FS_MAGIC["f2fs"] == (
            1024,
            (0xF2F52010).to_bytes(4, "little"),
        )
        assert _REPLAYING_FS_MAGIC["xfs"] == (0, b"XFSB")
        assert _REPLAYING_FS_MAGIC["btrfs"] == (0x10040, b"_BHRfS_M")

    def test_host_mount_ro_ext4_uses_noload(self):
        ctrl = SDWireController("s")
        mounts = []
        with (
            patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
            patch(
                "subprocess.run",
                side_effect=self._fake_run(lsblk="ext4", log=mounts),
            ),
        ):
            with ctrl.host_mount(2, mode="ro"):
                pass

        opts = mounts[0].split(",")
        assert opts[0] == "ro" and "noload" in opts

    def test_undeterminable_type_mount_failure_is_reported(self):
        """The reviewer's race: no blind retry with plain ro after a failure."""
        ctrl = SDWireController("s")
        mounts = []
        run = self._fake_run(
            lsblk="", head=b"", head_rc=1, mount_fails=True, log=mounts
        )
        with (
            patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
            patch("subprocess.run", side_effect=run),
        ):
            with pytest.raises(RuntimeError, match="Failed to mount"):
                with ctrl.host_mount(2, mode="ro"):
                    pass

        assert len(mounts) == 1 and "noload" in mounts[0]

    def test_rw_mount_unchanged(self):
        ctrl = SDWireController("s")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock()

        with (
            patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with ctrl.host_mount(1, mode="rw"):
                pass

        assert not any(c[0] == "lsblk" for c in calls)
        mount = next(c for c in calls if c[:2] == ["sudo", "mount"])
        assert "ro" not in mount[mount.index("-o") + 1].split(",")


class TestBlockDeviceHelpers:
    """Tests for block device validation helpers."""

    def test_block_device_has_media_true(self):
        from labctl.sdwire.controller import _block_device_has_media

        with patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(
                        return_value=MagicMock(
                            read=MagicMock(return_value="61071360\n"),
                            strip=MagicMock(return_value="61071360"),
                        )
                    ),
                    __exit__=MagicMock(return_value=False),
                )
            ),
        ):
            with patch("os.path.exists", return_value=True):
                assert _block_device_has_media("/dev/sdd") is True

    def test_block_device_has_media_zero(self):
        from labctl.sdwire.controller import _block_device_has_media

        with patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(
                        return_value=MagicMock(
                            read=MagicMock(return_value="0\n"),
                            strip=MagicMock(return_value="0"),
                        )
                    ),
                    __exit__=MagicMock(return_value=False),
                )
            ),
        ):
            with patch("os.path.exists", return_value=True):
                assert _block_device_has_media("/dev/sdc") is False

    def test_block_device_has_media_missing(self):
        from labctl.sdwire.controller import _block_device_has_media

        assert _block_device_has_media(None) is False
        with patch("os.path.exists", return_value=False):
            assert _block_device_has_media("/dev/nonexistent") is False


class TestDiscoverSDWireDevices:
    """Tests for discover_sdwire_devices."""

    def test_discover_returns_sdwirec(self):
        """Test discover returns SDWireC (Realtek) devices."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_dev = MagicMock()
        mock_dev.serial_string = "sdwirec_001"
        mock_dev.product_string = ""
        mock_dev.manufacturer_string = ""
        mock_dev.block_dev = "/dev/sdb"

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[]):
            with patch(
                "sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dev]
            ):
                result = discover_sdwire_devices()

        assert len(result) == 1
        assert result[0]["serial_number"] == "sdwirec_001"
        assert result[0]["device_type"] == "sdwirec"

    def test_discover_returns_legacy_sdwire(self):
        """Test discover returns legacy SDWire (FTDI) devices."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_dev = MagicMock()
        mock_dev.serial_string = "sd-wire_1"
        mock_dev.product_string = "sd-wire"
        mock_dev.manufacturer_string = "SRPOL"
        mock_dev.block_dev = "/dev/sdc"

        with patch(
            "sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]
        ):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                result = discover_sdwire_devices()

        assert len(result) == 1
        assert result[0]["serial_number"] == "sd-wire_1"
        assert result[0]["device_type"] == "sdwire"

    def test_discover_empty(self):
        """Test discover returns empty list when no devices connected."""
        from labctl.sdwire.controller import discover_sdwire_devices

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                result = discover_sdwire_devices()

        assert result == []

    def test_discover_both_types(self):
        """Test discover finds both SDWireC and legacy SDWire devices."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_legacy = MagicMock()
        mock_legacy.serial_string = "sd-wire_1"
        mock_legacy.product_string = "sd-wire"
        mock_legacy.manufacturer_string = "SRPOL"
        mock_legacy.block_dev = "/dev/sdc"

        mock_sdwirec = MagicMock()
        mock_sdwirec.serial_string = "sdwirec_001"
        mock_sdwirec.product_string = ""
        mock_sdwirec.manufacturer_string = ""
        mock_sdwirec.block_dev = "/dev/sdd"

        with patch(
            "sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_legacy]
        ):
            with patch(
                "sdwire.backend.detect.get_sdwire_devices", return_value=[mock_sdwirec]
            ):
                result = discover_sdwire_devices()

        assert len(result) == 2
        types = {d["device_type"] for d in result}
        assert types == {"sdwire", "sdwirec"}

    def test_discover_deduplicates(self):
        """Test discover deduplicates devices returned by both functions."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_dev = MagicMock()
        mock_dev.serial_string = "sd-wire_1"
        mock_dev.product_string = "sd-wire"
        mock_dev.manufacturer_string = "SRPOL"
        mock_dev.block_dev = "/dev/sdc"

        mock_dup = MagicMock()
        mock_dup.serial_string = "sd-wire_1"
        mock_dup.product_string = "sd-wire"
        mock_dup.manufacturer_string = "SRPOL"
        mock_dup.block_dev = "/dev/sdc"

        with patch(
            "sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]
        ):
            with patch(
                "sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dup]
            ):
                result = discover_sdwire_devices()

        assert len(result) == 1
        assert result[0]["device_type"] == "sdwire"

    def test_discover_skips_empty_serial(self):
        """Test that devices with empty serial numbers are skipped."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_dev = MagicMock()
        mock_dev.serial_string = ""
        mock_dev.product_string = ""
        mock_dev.manufacturer_string = ""
        mock_dev.block_dev = None

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[]):
            with patch(
                "sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dev]
            ):
                result = discover_sdwire_devices()

        assert result == []

    # A None entry in sys.modules makes `import sdwire...` raise ImportError,
    # simulating the optional package being absent (e.g. on Python < 3.12).
    _SDWIRE_ABSENT = {
        "sdwire": None,
        "sdwire.backend": None,
        "sdwire.backend.detect": None,
    }

    def test_discover_import_error(self):
        """discover_sdwire_devices raises a helpful error without sdwire."""
        from labctl.sdwire.controller import discover_sdwire_devices

        with patch.dict("sys.modules", self._SDWIRE_ABSENT):
            with pytest.raises(RuntimeError, match="sdwire package not installed"):
                discover_sdwire_devices()

    def test_get_device_import_error(self):
        """SDWireController._get_device raises a helpful error without sdwire."""
        ctrl = SDWireController("any_serial")

        with patch.dict("sys.modules", self._SDWIRE_ABSENT):
            with pytest.raises(RuntimeError, match="sdwire package not installed"):
                ctrl._get_device()


class TestValidation:
    """Tests for block device and image file validation."""

    def test_validate_block_device_zero_size(self):
        from labctl.sdwire.controller import _validate_block_device

        with patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(
                        return_value=MagicMock(read=MagicMock(return_value="0\n"))
                    ),
                    __exit__=MagicMock(return_value=False),
                )
            ),
        ):
            with pytest.raises(RuntimeError, match="0 size"):
                _validate_block_device("/dev/sdb")

    def test_validate_block_device_too_large(self):
        from labctl.sdwire.controller import _validate_block_device

        # 512 GB in 512-byte sectors
        huge_sectors = str(512 * 1024 * 1024 * 1024 // 512)
        with patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(
                        return_value=MagicMock(
                            read=MagicMock(return_value=huge_sectors + "\n")
                        )
                    ),
                    __exit__=MagicMock(return_value=False),
                )
            ),
        ):
            with pytest.raises(RuntimeError, match="too large"):
                _validate_block_device("/dev/sdb")

    def test_validate_block_device_mounted(self):
        from labctl.sdwire.controller import _validate_block_device

        # 32 GB in sectors
        sectors = str(32 * 1024 * 1024 * 1024 // 512)
        proc_mounts = "/dev/sdb1 /mnt/sd vfat rw 0 0\n"

        def mock_open(path, *args, **kwargs):
            m = MagicMock()
            if "/sys/block" in str(path):
                m.__enter__ = MagicMock(
                    return_value=MagicMock(read=MagicMock(return_value=sectors + "\n"))
                )
            else:  # /proc/mounts
                m.__enter__ = MagicMock(
                    return_value=MagicMock(
                        read=MagicMock(return_value=proc_mounts),
                        __iter__=MagicMock(return_value=iter(proc_mounts.splitlines())),
                    )
                )
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch("builtins.open", side_effect=mock_open):
            with pytest.raises(RuntimeError, match="mounted"):
                _validate_block_device("/dev/sdb")

    def test_validate_block_device_valid(self):
        from labctl.sdwire.controller import _validate_block_device

        # 32 GB, not mounted
        sectors = str(32 * 1024 * 1024 * 1024 // 512)

        def mock_open(path, *args, **kwargs):
            m = MagicMock()
            if "/sys/block" in str(path):
                m.__enter__ = MagicMock(
                    return_value=MagicMock(read=MagicMock(return_value=sectors + "\n"))
                )
            else:
                m.__enter__ = MagicMock(
                    return_value=MagicMock(
                        read=MagicMock(return_value=""),
                        __iter__=MagicMock(return_value=iter([])),
                    )
                )
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch("builtins.open", side_effect=mock_open):
            _validate_block_device("/dev/sdb")  # Should not raise

    def test_validate_image_file_not_found(self):
        from labctl.sdwire.controller import _validate_image_file

        with pytest.raises(RuntimeError, match="not found"):
            _validate_image_file("/nonexistent/image.img")

    def test_validate_image_file_unsupported_format(self, tmp_path):
        from labctl.sdwire.controller import _validate_image_file

        bad = tmp_path / "image.zip"
        bad.touch()
        with pytest.raises(RuntimeError, match="Unsupported"):
            _validate_image_file(str(bad))

    def test_validate_image_file_valid(self, tmp_path):
        from labctl.sdwire.controller import _validate_image_file

        for ext in [".img", ".img.xz", ".img.gz"]:
            f = tmp_path / f"test{ext}"
            f.touch()
            _validate_image_file(str(f))  # Should not raise


class TestReadAccess:
    """Tests for read-only SDWire operations."""

    def test_list_files_recursive(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        (root / "config.txt").write_text("hello")
        subdir = root / "nested"
        subdir.mkdir()
        (subdir / "cmdline.txt").write_text("root=/dev/mmcblk0p2")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            result = ctrl.list_files(partition=1, path="/", recursive=True)

        assert result["truncated"] is False
        assert result["_truncated"] is False
        paths = {entry["path"] for entry in result["entries"]}
        assert "/config.txt" in paths
        assert "/nested" in paths
        assert "/nested/cmdline.txt" in paths

    def test_list_files_truncates(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        for idx in range(3):
            (root / f"file{idx}.txt").write_text("x")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            result = ctrl.list_files(partition=1, max_entries=2)

        assert result["truncated"] is True
        assert result["_truncated"] is True
        assert len(result["entries"]) == 2

    def test_read_file_text(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        file_path = root / "autoboot.txt"
        file_path.write_text("BOOT_UART=1\n")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            result = ctrl.read_file(partition=1, path="/autoboot.txt")

        assert result["content"] == "BOOT_UART=1\n"
        assert result["encoding"] == "text"
        assert result["truncated"] is False
        assert result["size"] == len("BOOT_UART=1\n")

    def test_read_file_binary_requires_non_text_encoding(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        (root / "kernel8.img").write_bytes(b"\xff\x00")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            with pytest.raises(ValueError, match="binary_content"):
                ctrl.read_file(partition=1, path="/kernel8.img")

    def test_read_file_size_limit(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        (root / "large.bin").write_bytes(b"123456")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            with pytest.raises(RuntimeError, match="exceeds max_bytes"):
                ctrl.read_file(partition=1, path="/large.bin", max_bytes=4)

    def test_read_file_rejects_symlink(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        (root / "target.txt").write_text("secret")
        (root / "link.txt").symlink_to(root / "target.txt")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            with pytest.raises(RuntimeError, match="symlink"):
                ctrl.read_file(partition=1, path="/link.txt", encoding="base64")

    def test_list_files_permission_denied(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            with patch("os.scandir", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError, match="/"):
                    ctrl.list_files(partition=1, path="/")

    def test_read_file_permission_denied(self, tmp_path):
        ctrl = SDWireController("test_serial")
        root = tmp_path / "mount"
        root.mkdir()
        (root / "shadow").write_text("x")

        @contextmanager
        def fake_mount(*args, **kwargs):
            from labctl.sdwire.controller import _MountedPartition

            yield _MountedPartition(str(root))

        with patch.object(ctrl, "host_mount", fake_mount):
            with patch("builtins.open", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError, match="/shadow"):
                    ctrl.read_file(partition=1, path="/shadow")

    def test_resolve_path_rejects_leaf_symlink_to_outside(self, tmp_path):
        from labctl.sdwire.controller import _MountedPartition

        root = tmp_path / "mount"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "host.txt").write_text("host")
        (root / "linkdir").symlink_to(outside)

        mount = _MountedPartition(str(root))
        with pytest.raises(RuntimeError, match="escapes partition|target escapes"):
            mount.resolve_path("/linkdir")

    def test_parse_parted_output_free_regions(self):
        from labctl.sdwire.controller import _parse_parted_output

        output = """BYT;
/dev/sdb:1024MiB:scsi:512:512:msdos:Test Disk:;
1:0.02MiB:1.00MiB:0.98MiB:free;
1:1.00MiB:257.00MiB:256.00MiB:fat32:BOOT:lba;
2:257.00MiB:1023.00MiB:766.00MiB:ext4:rootfs:;
3:1023.00MiB:1024.00MiB:1.00MiB:free;
"""

        blkid_outputs = {
            "/dev/sdb": "PTTYPE=msdos\n",
            "/dev/sdb1": "PARTUUID=abcd-01\nUUID=54D4-4272\nLABEL=BOOT\n",
            "/dev/sdb2": "PARTUUID=abcd-02\nUUID=root-uuid\nLABEL=rootfs\n",
        }

        def fake_run(cmd, check=False, capture_output=True, text=True):
            mock = MagicMock()
            mock.stdout = blkid_outputs.get(cmd[-1], "")
            return mock

        with patch("labctl.sdwire.controller.subprocess.run", side_effect=fake_run):
            with patch("labctl.sdwire.controller._is_mounted", return_value=False):
                result = _parse_parted_output("/dev/sdb", output)

        assert result["disklabel_type"] == "msdos"
        assert len(result["partitions"]) == 2
        assert result["partitions"][0]["num"] == 1
        assert result["partitions"][0]["flags"] == ["lba"]
        assert result["partitions"][1]["num"] == 2
        assert result["partitions"][1]["flags"] == []
        assert result["free_space_regions"] == [
            {"start_mib": 0.02, "end_mib": 1.0, "size_mib": 0.98},
            {"start_mib": 1023.0, "end_mib": 1024.0, "size_mib": 1.0},
        ]

    def test_get_disk_info_reports_missing_parted(self):
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch(
                "labctl.sdwire.controller.subprocess.run",
                side_effect=FileNotFoundError("parted"),
            ):
                with pytest.raises(
                    RuntimeError, match="Failed to read partition table"
                ):
                    ctrl.get_disk_info()
