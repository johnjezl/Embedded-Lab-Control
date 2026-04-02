"""
SDWire controller for switching SD cards between DUT and host.

Wraps the sdwire Python library to control SDWire devices.

Supported device types:
  - sdwirec: SDWireC (Realtek-based, VID 0BDA:0316) — newer, faster
  - sdwire:  Legacy SDWire (FTDI-based, VID 04E8:6001)
"""

import logging
import os
import shutil
import subprocess
import tempfile
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
        logger.debug("Switching SDWire %s to DUT", self.serial_number)
        device = self._get_device()
        device.switch_dut()
        logger.debug("SDWire %s switched to DUT", self.serial_number)

    def switch_to_host(self) -> None:
        """Switch SD card to the host (dev machine can read/write it)."""
        logger.debug("Switching SDWire %s to host", self.serial_number)
        device = self._get_device()
        device.switch_ts()
        logger.debug("SDWire %s switched to host", self.serial_number)

    def get_block_device(self, settle_time: float = 0) -> Optional[str]:
        """Get the block device path when SD card is connected to host.

        Validates the device has actual media (non-zero size) to avoid
        returning stale device nodes from previous enumerations.

        Args:
            settle_time: Seconds to wait for device to settle (0 = no wait)

        Returns /dev/sdX path or None if not available.
        """
        import time

        if settle_time > 0:
            time.sleep(settle_time)

        try:
            device = self._get_device()
            block_dev = getattr(device, "block_dev", None)

            if block_dev and _block_device_has_media(block_dev):
                return block_dev

            if block_dev:
                logger.debug(
                    "Block device %s has no media (stale node)", block_dev
                )

            return None

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
                    "sudo", "dd",
                    f"if={image_path}",
                    f"of={block_dev}",
                    f"bs={block_size}",
                    "status=progress",
                    "conv=fsync",
                ],
                check=True,
            )
            subprocess.run(["sudo", "sync"], check=True)
            return True
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Flash failed: {e}") from e

    def update_files(
        self,
        partition: int,
        file_pairs: list[tuple[str, str]],
        renames: list[tuple[str, str]] | None = None,
        deletes: list[str] | None = None,
    ) -> dict[str, list[str]]:
        """Copy, rename, and/or delete files on a partition on the SD card.

        Mounts the partition, performs operations in order (copies, renames,
        deletes), unmounts. Assumes SD card is already switched to host mode.

        Args:
            partition: Partition number (e.g., 1 for /dev/sdb1)
            file_pairs: List of (source_path, dest_path_relative_to_partition_root)
            renames: List of (old_name, new_name) relative to partition root
            deletes: List of filenames relative to partition root

        Returns:
            Dict with keys "copied", "renamed", "deleted", each a list of strings.

        Raises:
            RuntimeError: If block device not found, mount fails, or any op fails.
        """
        block_dev = self.get_block_device()
        if not block_dev:
            raise RuntimeError(
                "Cannot determine block device. "
                "Is the SD card switched to host mode?"
            )

        part_dev = f"{block_dev}{partition}"
        mount_point = tempfile.mkdtemp(prefix="labctl-sdwire-")

        logger.info("Mounting %s at %s", part_dev, mount_point)

        try:
            subprocess.run(
                [
                    "sudo", "mount",
                    "-o", f"uid={os.getuid()},gid={os.getgid()}",
                    part_dev, mount_point,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            os.rmdir(mount_point)
            raise RuntimeError(
                f"Failed to mount {part_dev}: {e.stderr.strip()}"
            ) from e

        result = {"copied": [], "renamed": [], "deleted": []}
        try:
            # 1. Copies
            for src, dest_relative in file_pairs:
                dest = os.path.join(mount_point, dest_relative)

                dest_dir = os.path.dirname(dest)
                if dest_dir and not os.path.exists(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)

                logger.info("Copying %s -> %s", src, dest_relative)
                shutil.copy2(src, dest)
                result["copied"].append(dest_relative)

            # 2. Renames
            for old_name, new_name in (renames or []):
                old_path = os.path.join(mount_point, old_name)
                new_path = os.path.join(mount_point, new_name)

                if not os.path.exists(old_path):
                    raise RuntimeError(
                        f"Rename failed: '{old_name}' not found on partition"
                    )
                if os.path.exists(new_path):
                    raise RuntimeError(
                        f"Rename failed: '{new_name}' already exists on partition"
                    )

                new_dir = os.path.dirname(new_path)
                if new_dir and not os.path.exists(new_dir):
                    os.makedirs(new_dir, exist_ok=True)

                logger.info("Renaming %s -> %s", old_name, new_name)
                os.rename(old_path, new_path)
                result["renamed"].append(f"{old_name} -> {new_name}")

            # 3. Deletes
            for filename in (deletes or []):
                file_path = os.path.join(mount_point, filename)

                if not os.path.exists(file_path):
                    raise RuntimeError(
                        f"Delete failed: '{filename}' not found on partition"
                    )
                if os.path.isdir(file_path):
                    raise RuntimeError(
                        f"Delete failed: '{filename}' is a directory, not a file"
                    )

                logger.info("Deleting %s", filename)
                os.remove(file_path)
                result["deleted"].append(filename)

        finally:
            logger.info("Unmounting %s", mount_point)
            subprocess.run(
                ["sudo", "umount", mount_point],
                capture_output=True,
            )
            try:
                os.rmdir(mount_point)
            except OSError:
                pass

        subprocess.run(["sudo", "sync"], check=True)
        return result


def _block_device_has_media(block_dev: str) -> bool:
    """Check if a block device has actual media (non-zero size)."""
    if not block_dev or not os.path.exists(block_dev):
        return False
    size_path = f"/sys/block/{os.path.basename(block_dev)}/size"
    try:
        with open(size_path) as f:
            return int(f.read().strip()) > 0
    except (OSError, ValueError):
        return False


def discover_sdwire_devices() -> list[dict]:
    """Discover all connected SDWire devices.

    Returns a list of dicts with serial_number, device_type, and block_dev.
    Detects both SDWireC (Realtek) and legacy SDWire (FTDI) devices.
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
    seen_serials = set()

    # Legacy SDWire (FTDI-based, VID 04E8:6001)
    try:
        for dev in get_sdwirec_devices():
            serial = dev.serial_string
            if serial and serial not in seen_serials:
                seen_serials.add(serial)
                results.append({
                    "serial_number": serial,
                    "device_type": "sdwire",
                    "product": getattr(dev, "product_string", ""),
                    "manufacturer": getattr(dev, "manufacturer_string", ""),
                    "block_dev": getattr(dev, "block_dev", None),
                })
    except Exception:
        pass

    # SDWireC (Realtek-based, VID 0BDA:0316)
    # get_sdwire_devices() also returns legacy devices, so deduplicate
    try:
        for dev in get_sdwire_devices():
            serial = dev.serial_string
            if serial and serial not in seen_serials:
                seen_serials.add(serial)
                results.append({
                    "serial_number": serial,
                    "device_type": "sdwirec",
                    "product": getattr(dev, "product_string", ""),
                    "manufacturer": getattr(dev, "manufacturer_string", ""),
                    "block_dev": getattr(dev, "block_dev", None),
                })
    except Exception:
        pass

    return results
