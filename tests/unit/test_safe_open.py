"""Tests for labctl.core.safe_open and descriptor-based host I/O.

Review #5 of #14: the allowlist resolved a path, but files were re-opened by
path later (after power-off, mux switch, waits), so a symlink swapped in
meanwhile could redirect the I/O; raw images were even read by `sudo dd`.
Now files are opened at check time without following any symlink, and all
I/O goes through the descriptor. These tests use real files and links.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from labctl.core.safe_open import open_dir_nofollow, open_file_nofollow


class TestOpenFileNoFollow:
    def test_opens_regular_file(self, tmp_path):
        f = tmp_path / "img"
        f.write_text("data")
        fd = open_file_nofollow(str(f))
        try:
            assert os.read(fd, 10) == b"data"
        finally:
            os.close(fd)

    def test_final_symlink_refused(self, tmp_path):
        (tmp_path / "secret").write_text("s")
        (tmp_path / "link").symlink_to(tmp_path / "secret")
        with pytest.raises(OSError):
            open_file_nofollow(str(tmp_path / "link"))

    def test_intermediate_symlink_refused(self, tmp_path):
        """O_NOFOLLOW alone only guards the last component; the walk guards
        every directory on the path."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "f").write_text("x")
        (tmp_path / "dirlink").symlink_to(real)
        with pytest.raises(OSError):
            open_file_nofollow(str(tmp_path / "dirlink" / "f"))

    @pytest.mark.parametrize("bad", ["relative/f", "/a/../b"])
    def test_unnormalized_paths_rejected(self, bad):
        with pytest.raises((ValueError, OSError)):
            open_file_nofollow(bad)


class TestOpenDirNoFollow:
    def test_creates_missing_components(self, tmp_path):
        target = tmp_path / "out" / "t1"
        fd = open_dir_nofollow(str(target), create=True)
        os.close(fd)
        assert target.is_dir()

    def test_symlinked_dir_refused(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / "t1").symlink_to(elsewhere)
        with pytest.raises(OSError):
            open_dir_nofollow(str(tmp_path / "t1"), create=True)


class TestSwapAfterCheck:
    """The reviewer's scenarios, end to end on a real filesystem."""

    def test_read_swap_after_check_is_not_followed(self, tmp_path):
        """Scenario 2: checked image replaced by a symlink before the read."""
        from labctl.core.config import Config
        from labctl.mcp_server import _open_host_read

        images = tmp_path / "images"
        images.mkdir()
        img = images / "foo.img"
        img.write_text("the image that was checked")
        secret = tmp_path / "secret"
        secret.write_text("root-only secret")
        config = Config()
        config.mcp.allowed_read_paths = [str(images)]

        with patch("labctl.mcp_server._get_config", return_value=config):
            fd, err = _open_host_read(str(img))
        assert err is None
        try:
            # Attacker swaps the checked file for a symlink to a secret.
            img.unlink()
            img.symlink_to(secret)
            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, 100) == b"the image that was checked"
        finally:
            os.close(fd)

    def test_output_dir_swap_after_open_is_not_followed(self, tmp_path):
        """Scenario 1: output dir replaced by a symlink after it was opened."""
        from labctl.serial.boot_test import run_boot_test
        from labctl.serial.capture import CaptureResult

        out = tmp_path / "output" / "t1"
        victim_dir = tmp_path / "service-config"
        victim_dir.mkdir()
        (victim_dir / "run_01.txt").write_text("precious")
        dir_fd = open_dir_nofollow(str(out), create=True)
        try:
            # Attacker moves the opened dir away and plants a symlink.
            out.rename(tmp_path / "moved")
            out.symlink_to(victim_dir)
            with patch("labctl.serial.boot_test.capture_serial_output") as cap:
                cap.return_value = CaptureResult(
                    output="console", lines=1, pattern_matched=True, elapsed_seconds=1
                )
                run_boot_test(
                    sbc_name="s",
                    expect_pattern="console",
                    tcp_host="localhost",
                    tcp_port=4000,
                    power_cycle_fn=MagicMock(),
                    runs=1,
                    timeout=1.0,
                    output_dir=str(out),
                    output_dir_fd=dir_fd,
                )
        finally:
            os.close(dir_fd)

        assert (victim_dir / "run_01.txt").read_text() == "precious"
        assert (tmp_path / "moved" / "run_01.txt").read_text() == "console"


class TestControllerReadsDescriptors:
    def test_raw_flash_feeds_dd_from_descriptor(self, tmp_path):
        """dd gets no if=<path>: root never opens a host path."""
        from labctl.sdwire.controller import SDWireController

        img = tmp_path / "x.img"
        img.write_bytes(b"\0" * 10)
        fd = os.open(img, os.O_RDONLY)
        ctrl = SDWireController("s")
        try:
            with (
                patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
                patch("labctl.sdwire.controller._validate_block_device"),
                patch("labctl.sdwire.controller.subprocess.run") as run,
            ):
                result = ctrl.flash_image(str(img), image_fd=fd)
        finally:
            os.close(fd)

        dd_call = run.call_args_list[0]
        assert dd_call.args[0][:2] == ["sudo", "dd"]
        assert not any(a.startswith("if=") for a in dd_call.args[0])
        assert dd_call.kwargs["stdin"] == fd
        assert result["bytes_written"] == 10

    def test_update_files_copies_from_descriptor(self, tmp_path):
        from contextlib import contextmanager

        from labctl.sdwire.controller import SDWireController, _MountedPartition

        card = tmp_path / "card"
        (card / "boot").mkdir(parents=True)
        src = tmp_path / "Image-6.1"
        src.write_text("kernel")
        fd = os.open(src, os.O_RDONLY)
        ctrl = SDWireController("s")

        @contextmanager
        def fake_mount(partition, mode="ro", owner_mount=False):
            yield _MountedPartition(str(card), owner_mount=owner_mount)

        try:
            with (
                patch.object(ctrl, "get_block_device", return_value="/dev/sdx"),
                patch.object(ctrl, "host_mount", fake_mount),
            ):
                ctrl.update_files(1, [(fd, "boot")], source_names=["Image"])
        finally:
            os.close(fd)

        assert (card / "boot" / "Image").read_text() == "kernel"
