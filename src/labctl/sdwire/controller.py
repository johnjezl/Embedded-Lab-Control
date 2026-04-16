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
                logger.debug("Block device %s has no media (stale node)", block_dev)

            return None

        except RuntimeError:
            return None

    def flash_image(
        self,
        image_path: str,
        block_size: str = "4M",
        timeout: int = 1800,
    ) -> dict:
        """Write an image to the SD card.

        Supports raw .img, .img.xz, and .img.gz files. Assumes the SD
        card is already switched to host mode.

        Args:
            image_path: Path to the image file
            block_size: Block size for dd (default: 4M)
            timeout: Max seconds for the write (default: 1800 / 30 min)

        Returns:
            Dict with bytes_written and elapsed_seconds.

        Raises:
            RuntimeError: On validation failure or write error.
        """
        import time as time_mod

        block_dev = self.get_block_device()
        if not block_dev:
            raise RuntimeError(
                "Cannot determine block device. "
                "Is the SD card switched to host mode?"
            )

        # Safety checks
        _validate_block_device(block_dev)
        _validate_image_file(image_path)

        logger.info(
            "Flashing %s to %s via SDWire %s",
            image_path,
            block_dev,
            self.serial_number,
        )

        start = time_mod.monotonic()

        try:
            if image_path.endswith(".xz"):
                # Pipe: xz -dc image | sudo dd of=dev bs=4M oflag=sync
                decompress = subprocess.Popen(
                    ["xz", "-dc", image_path],
                    stdout=subprocess.PIPE,
                )
                dd = subprocess.Popen(
                    [
                        "sudo",
                        "dd",
                        f"of={block_dev}",
                        f"bs={block_size}",
                        "oflag=sync",
                        "status=progress",
                    ],
                    stdin=decompress.stdout,
                    stderr=subprocess.PIPE,
                )
                decompress.stdout.close()
                _, dd_stderr = dd.communicate(timeout=timeout)
                if decompress.wait() != 0:
                    raise RuntimeError("xz decompression failed")
                if dd.returncode != 0:
                    raise RuntimeError(
                        f"dd failed: {dd_stderr.decode('utf-8', errors='replace')}"
                    )
            elif image_path.endswith(".gz"):
                decompress = subprocess.Popen(
                    ["gzip", "-dc", image_path],
                    stdout=subprocess.PIPE,
                )
                dd = subprocess.Popen(
                    [
                        "sudo",
                        "dd",
                        f"of={block_dev}",
                        f"bs={block_size}",
                        "oflag=sync",
                        "status=progress",
                    ],
                    stdin=decompress.stdout,
                    stderr=subprocess.PIPE,
                )
                decompress.stdout.close()
                _, dd_stderr = dd.communicate(timeout=timeout)
                if decompress.wait() != 0:
                    raise RuntimeError("gzip decompression failed")
                if dd.returncode != 0:
                    raise RuntimeError(
                        f"dd failed: {dd_stderr.decode('utf-8', errors='replace')}"
                    )
            else:
                subprocess.run(
                    [
                        "sudo",
                        "dd",
                        f"if={image_path}",
                        f"of={block_dev}",
                        f"bs={block_size}",
                        "status=progress",
                        "conv=fsync",
                    ],
                    check=True,
                    timeout=timeout,
                )

            subprocess.run(["sudo", "sync"], check=True)

            elapsed = time_mod.monotonic() - start
            image_size = os.path.getsize(image_path)

            return {
                "bytes_written": image_size,
                "elapsed_seconds": round(elapsed, 1),
                "block_device": block_dev,
            }
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Flash timed out after {timeout}s")
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
                    "sudo",
                    "mount",
                    "-o",
                    f"uid={os.getuid()},gid={os.getgid()}",
                    part_dev,
                    mount_point,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            os.rmdir(mount_point)
            raise RuntimeError(f"Failed to mount {part_dev}: {e.stderr.strip()}") from e

        real_mount = os.path.realpath(mount_point)

        def _safe_path(relative: str) -> str:
            """Resolve path and verify it stays under mount_point."""
            full = os.path.realpath(os.path.join(mount_point, relative))
            if not full.startswith(real_mount + "/") and full != real_mount:
                raise RuntimeError(
                    f"Path traversal rejected: '{relative}' escapes partition"
                )
            return full

        result = {"copied": [], "renamed": [], "deleted": []}
        try:
            # 1. Copies
            for src, dest_relative in file_pairs:
                dest = _safe_path(dest_relative)

                dest_dir = os.path.dirname(dest)
                if dest_dir and not os.path.exists(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)

                logger.info("Copying %s -> %s", src, dest_relative)
                shutil.copy2(src, dest)
                result["copied"].append(dest_relative)

            # 2. Renames
            for old_name, new_name in renames or []:
                old_path = _safe_path(old_name)
                new_path = _safe_path(new_name)

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
            for filename in deletes or []:
                file_path = _safe_path(filename)

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


def _validate_block_device(block_dev: str) -> None:
    """Validate a block device is safe to write to.

    Checks:
    - Device has media (non-zero size)
    - Device is not too large (max 256 GB — not a system disk)
    - No partitions are currently mounted

    Raises:
        RuntimeError: If any check fails.
    """
    dev_name = os.path.basename(block_dev)
    size_path = f"/sys/block/{dev_name}/size"

    try:
        with open(size_path) as f:
            sectors = int(f.read().strip())
    except (OSError, ValueError):
        raise RuntimeError(f"Cannot read size of {block_dev}")

    if sectors == 0:
        raise RuntimeError(f"Block device {block_dev} reports 0 size (no media?)")

    # 512 bytes per sector; 256 GB max
    size_bytes = sectors * 512
    max_bytes = 256 * 1024 * 1024 * 1024
    if size_bytes > max_bytes:
        size_gb = size_bytes / (1024**3)
        raise RuntimeError(
            f"Block device {block_dev} is {size_gb:.0f} GB — too large for SD card "
            f"(max 256 GB). Refusing to write for safety."
        )

    # Check no partitions are mounted
    try:
        with open("/proc/mounts") as f:
            mounts = f.read()
        if block_dev in mounts or f"{block_dev}p" in mounts:
            raise RuntimeError(
                f"Block device {block_dev} has mounted partitions. "
                f"Unmount before flashing."
            )
        # Also check numbered partitions (e.g., /dev/sdb1)
        for line in mounts.splitlines():
            mount_dev = line.split()[0] if line.strip() else ""
            if mount_dev.startswith(block_dev):
                raise RuntimeError(
                    f"Partition {mount_dev} is mounted. Unmount before flashing."
                )
    except OSError:
        pass  # /proc/mounts not available — skip check


def _validate_image_file(image_path: str) -> None:
    """Validate an image file before flashing.

    Raises:
        RuntimeError: If the file is invalid.
    """
    if not os.path.exists(image_path):
        raise RuntimeError(f"Image file not found: {image_path}")
    if not os.path.isfile(image_path):
        raise RuntimeError(f"Not a regular file: {image_path}")

    supported = (".img", ".img.xz", ".img.gz")
    if not any(image_path.endswith(ext) for ext in supported):
        raise RuntimeError(
            f"Unsupported image format: {image_path}. "
            f"Supported: {', '.join(supported)}"
        )


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
                results.append(
                    {
                        "serial_number": serial,
                        "device_type": "sdwire",
                        "product": getattr(dev, "product_string", ""),
                        "manufacturer": getattr(dev, "manufacturer_string", ""),
                        "block_dev": getattr(dev, "block_dev", None),
                    }
                )
    except Exception:
        pass

    # SDWireC (Realtek-based, VID 0BDA:0316)
    # get_sdwire_devices() also returns legacy devices, so deduplicate
    try:
        for dev in get_sdwire_devices():
            serial = dev.serial_string
            if serial and serial not in seen_serials:
                seen_serials.add(serial)
                results.append(
                    {
                        "serial_number": serial,
                        "device_type": "sdwirec",
                        "product": getattr(dev, "product_string", ""),
                        "manufacturer": getattr(dev, "manufacturer_string", ""),
                        "block_dev": getattr(dev, "block_dev", None),
                    }
                )
    except Exception:
        pass

    return results
