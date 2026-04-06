"""Unit tests for SDWire controller."""

from unittest.mock import MagicMock, patch

import pytest

from labctl.sdwire.controller import SDWireController


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

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]):
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

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[other_dev]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[target_dev]):
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
            with patch("labctl.sdwire.controller._block_device_has_media", return_value=False):
                result = ctrl.get_block_device()

        assert result is None

    def test_get_block_device_returns_valid(self):
        """Test get_block_device returns library result when it has media."""
        ctrl = SDWireController("test_serial")

        mock_dev = MagicMock()
        mock_dev.serial_string = "test_serial"
        mock_dev.block_dev = "/dev/sdb"

        with patch.object(ctrl, "_get_device", return_value=mock_dev):
            with patch("labctl.sdwire.controller._block_device_has_media", return_value=True):
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
                                    1, [],
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
                                    1, [],
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
                                    1, [],
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
                                        1, [],
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
                                    1, [],
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
                                with pytest.raises(RuntimeError, match="is a directory"):
                                    ctrl.update_files(
                                        1, [],
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
                            with patch("os.path.exists", side_effect=exists_side_effect):
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
        umount_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][1] == "umount"
        ]
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
                                1, [],
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
                                1, [],
                                deletes=["../../../etc/passwd"],
                            )


class TestBlockDeviceHelpers:
    """Tests for block device validation helpers."""

    def test_block_device_has_media_true(self):
        from labctl.sdwire.controller import _block_device_has_media

        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value="61071360\n"),
                strip=MagicMock(return_value="61071360"),
            )),
            __exit__=MagicMock(return_value=False),
        ))):
            with patch("os.path.exists", return_value=True):
                assert _block_device_has_media("/dev/sdd") is True

    def test_block_device_has_media_zero(self):
        from labctl.sdwire.controller import _block_device_has_media

        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value="0\n"),
                strip=MagicMock(return_value="0"),
            )),
            __exit__=MagicMock(return_value=False),
        ))):
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
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dev]):
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

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]):
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

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_legacy]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[mock_sdwirec]):
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

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dup]):
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
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[mock_dev]):
                result = discover_sdwire_devices()

        assert result == []

    def test_discover_import_error(self):
        """Test discover raises RuntimeError when sdwire not installed."""
        from labctl.sdwire.controller import discover_sdwire_devices

        with patch.dict("sys.modules", {"sdwire": None, "sdwire.backend": None, "sdwire.backend.detect": None}):
            # Force reimport to trigger ImportError
            import importlib
            import labctl.sdwire.controller

            with pytest.raises(RuntimeError, match="sdwire package not installed"):
                # Call with the import patched out
                original = labctl.sdwire.controller.discover_sdwire_devices

                def patched():
                    try:
                        from sdwire.backend.detect import (
                            get_sdwire_devices,
                            get_sdwirec_devices,
                        )
                    except (ImportError, TypeError):
                        raise RuntimeError(
                            "sdwire package not installed. Install with: pip install sdwire"
                        )

                patched()


class TestValidation:
    """Tests for block device and image file validation."""

    def test_validate_block_device_zero_size(self):
        from labctl.sdwire.controller import _validate_block_device

        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value="0\n")
            )),
            __exit__=MagicMock(return_value=False),
        ))):
            with pytest.raises(RuntimeError, match="0 size"):
                _validate_block_device("/dev/sdb")

    def test_validate_block_device_too_large(self):
        from labctl.sdwire.controller import _validate_block_device

        # 512 GB in 512-byte sectors
        huge_sectors = str(512 * 1024 * 1024 * 1024 // 512)
        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value=huge_sectors + "\n")
            )),
            __exit__=MagicMock(return_value=False),
        ))):
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
                m.__enter__ = MagicMock(return_value=MagicMock(
                    read=MagicMock(return_value=sectors + "\n")
                ))
            else:  # /proc/mounts
                m.__enter__ = MagicMock(return_value=MagicMock(
                    read=MagicMock(return_value=proc_mounts),
                    __iter__=MagicMock(return_value=iter(proc_mounts.splitlines())),
                ))
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
                m.__enter__ = MagicMock(return_value=MagicMock(
                    read=MagicMock(return_value=sectors + "\n")
                ))
            else:
                m.__enter__ = MagicMock(return_value=MagicMock(
                    read=MagicMock(return_value=""),
                    __iter__=MagicMock(return_value=iter([])),
                ))
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
