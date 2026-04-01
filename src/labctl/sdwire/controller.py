"""
SDWire controller for switching SD cards between DUT and host.

Wraps the sdwire Python library to control SDWire, SDWireC, and SDWire3 devices.
"""

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class SDWireController:
    """Controls an SDWire SD card multiplexer."""

    def __init__(self, serial_number: str, device_type: str = "sdwirec"):
        self.serial_number = serial_number
        self.device_type = device_type

    def _get_device(self):
        """Find and return the sdwire device object by serial number."""
        try:
            from sdwire.backend.detect import (
                get_sdwire_devices,
                get_sdwirec_devices,
            )
        except ImportError:
            raise RuntimeError(
                "sdwire package not installed. Install with: pip install sdwire"
            )

        # Search both device types
        all_devices = []
        try:
            all_devices.extend(get_sdwirec_devices())
        except Exception:
            pass
        try:
            all_devices.extend(get_sdwire_devices())
        except Exception:
            pass

        for dev in all_devices:
            if dev.serial_string == self.serial_number:
                return dev

        raise RuntimeError(
            f"SDWire device with serial '{self.serial_number}' not found. "
            "Is it connected?"
        )

    def switch_to_dut(self) -> None:
        """Switch SD card to the DUT (target SBC boots from it)."""
        logger.debug(
            "Switching SDWire %s to DUT", self.serial_number
        )
        device = self._get_device()
        device.switch_dut()
        logger.debug("SDWire %s switched to DUT", self.serial_number)

    def switch_to_host(self) -> None:
        """Switch SD card to the host (dev machine can read/write it)."""
        logger.debug(
            "Switching SDWire %s to host", self.serial_number
        )
        device = self._get_device()
        device.switch_ts()
        logger.debug("SDWire %s switched to host", self.serial_number)

    def get_block_device(self) -> Optional[str]:
        """Get the block device path when SD card is connected to host.

        Returns /dev/sdX path or None if not available.
        """
        try:
            device = self._get_device()
            return getattr(device, "block_dev", None)
        except RuntimeError:
            return None

    def flash_image(
        self,
        image_path: str,
        block_size: str = "4M",
    ) -> bool:
        """Write an image to the SD card.

        Assumes the SD card is already switched to host mode.

        Args:
            image_path: Path to the image file
            block_size: Block size for dd (default: 4M)

        Returns:
            True if successful
        """
        block_dev = self.get_block_device()
        if not block_dev:
            raise RuntimeError(
                "Cannot determine block device. "
                "Is the SD card switched to host mode?"
            )

        logger.info(
            "Flashing %s to %s via SDWire %s",
            image_path, block_dev, self.serial_number,
        )

        try:
            subprocess.run(
                [
                    "dd",
                    f"if={image_path}",
                    f"of={block_dev}",
                    f"bs={block_size}",
                    "status=progress",
                    "conv=fsync",
                ],
                check=True,
            )
            subprocess.run(["sync"], check=True)
            return True
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Flash failed: {e}") from e


def discover_sdwire_devices() -> list[dict]:
    """Discover all connected SDWire devices.

    Returns a list of dicts with serial_number, device_type, and block_dev.
    """
    try:
        from sdwire.backend.detect import (
            get_sdwire_devices,
            get_sdwirec_devices,
        )
    except ImportError:
        raise RuntimeError(
            "sdwire package not installed. Install with: pip install sdwire"
        )

    results = []

    try:
        for dev in get_sdwirec_devices():
            results.append({
                "serial_number": dev.serial_string,
                "device_type": "sdwirec",
                "product": getattr(dev, "product_string", ""),
                "manufacturer": getattr(dev, "manufacturer_string", ""),
                "block_dev": getattr(dev, "block_dev", None),
            })
    except Exception:
        pass

    try:
        for dev in get_sdwire_devices():
            results.append({
                "serial_number": dev.serial_string,
                "device_type": "sdwire3",
                "product": getattr(dev, "product_string", ""),
                "manufacturer": getattr(dev, "manufacturer_string", ""),
                "block_dev": getattr(dev, "block_dev", None),
            })
    except Exception:
        pass

    return results
