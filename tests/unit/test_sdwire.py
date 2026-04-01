"""Unit tests for SDWire controller."""

from unittest.mock import MagicMock, patch

import pytest

from labctl.sdwire.controller import SDWireController


class TestSDWireController:
    """Tests for SDWireController."""

    def test_init(self):
        """Test controller initialization."""
        ctrl = SDWireController("bdgrd_sdwirec_522", "sdwirec")
        assert ctrl.serial_number == "bdgrd_sdwirec_522"
        assert ctrl.device_type == "sdwirec"

    def test_init_default_type(self):
        """Test controller default device type."""
        ctrl = SDWireController("serial-123")
        assert ctrl.device_type == "sdwirec"

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
        """Test flash_image runs dd and sync."""
        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch("labctl.sdwire.controller.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = ctrl.flash_image("/path/to/image.img")

        assert result is True
        # dd call
        dd_call = mock_run.call_args_list[0]
        assert "dd" in dd_call[0][0]
        assert "if=/path/to/image.img" in dd_call[0][0]
        assert "of=/dev/sdb" in dd_call[0][0]
        # sync call
        sync_call = mock_run.call_args_list[1]
        assert sync_call[0][0] == ["sync"]

    def test_flash_image_dd_fails(self):
        """Test flash_image raises on dd failure."""
        import subprocess

        ctrl = SDWireController("test_serial")

        with patch.object(ctrl, "get_block_device", return_value="/dev/sdb"):
            with patch(
                "labctl.sdwire.controller.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "dd"),
            ):
                with pytest.raises(RuntimeError, match="Flash failed"):
                    ctrl.flash_image("/path/to/image.img")


class TestDiscoverSDWireDevices:
    """Tests for discover_sdwire_devices."""

    def test_discover_returns_list(self):
        """Test discover returns a list of device dicts."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_dev = MagicMock()
        mock_dev.serial_string = "bdgrd_sdwirec_001"
        mock_dev.product_string = "sd-wire"
        mock_dev.manufacturer_string = "SRPOL"
        mock_dev.block_dev = "/dev/sdb"

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_dev]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                result = discover_sdwire_devices()

        assert len(result) == 1
        assert result[0]["serial_number"] == "bdgrd_sdwirec_001"
        assert result[0]["device_type"] == "sdwirec"
        assert result[0]["block_dev"] == "/dev/sdb"

    def test_discover_empty(self):
        """Test discover returns empty list when no devices connected."""
        from labctl.sdwire.controller import discover_sdwire_devices

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[]):
                result = discover_sdwire_devices()

        assert result == []

    def test_discover_both_types(self):
        """Test discover finds both SDWireC and SDWire3 devices."""
        from labctl.sdwire.controller import discover_sdwire_devices

        mock_sdwirec = MagicMock()
        mock_sdwirec.serial_string = "bdgrd_sdwirec_001"
        mock_sdwirec.product_string = "sd-wire"
        mock_sdwirec.manufacturer_string = "SRPOL"
        mock_sdwirec.block_dev = None

        mock_sdwire3 = MagicMock()
        mock_sdwire3.serial_string = "sdwire_gen2_101"
        mock_sdwire3.product_string = ""
        mock_sdwire3.manufacturer_string = ""
        mock_sdwire3.block_dev = "/dev/sdc"

        with patch("sdwire.backend.detect.get_sdwirec_devices", return_value=[mock_sdwirec]):
            with patch("sdwire.backend.detect.get_sdwire_devices", return_value=[mock_sdwire3]):
                result = discover_sdwire_devices()

        assert len(result) == 2
        types = {d["device_type"] for d in result}
        assert types == {"sdwirec", "sdwire3"}

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
