"""Destructive CLI commands: terminal-only confirmation and --dry-run (WS3).

D012 / Phase 1 WS3: destructive commands ask before acting, but only on an
interactive terminal, so scripts, CI and agents (non-interactive stdin) run
exactly as before. ``--yes`` skips the question. ``power cycle``,
``sdwire flash`` and ``sdwire update`` also take ``--dry-run``.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from labctl.cli import main
from labctl.core.models import PlugType


@pytest.fixture
def lab(tmp_path):
    """Throwaway config + DB: pi-1 with a Tasmota plug and an SDWire."""
    from labctl.core.manager import get_manager

    db_path = tmp_path / "labctl.db"
    config = tmp_path / "config.yaml"
    config.write_text(f"database_path: {db_path}\n")
    manager = get_manager(db_path)
    sbc = manager.create_sbc(name="pi-1", project="t")
    manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "10.0.0.9", 2)
    dev = manager.create_sdwire_device(name="sdw-1", serial_number="bdgrd_sdwirec_9")
    manager.assign_sdwire(sbc.id, dev.id)
    power = MagicMock()
    power.power_off.return_value = True
    power.power_on.return_value = True
    with patch("labctl.power.base.PowerController.from_plug", return_value=power):
        yield config, power, tmp_path


def run(config, *args, input=None):
    return CliRunner().invoke(main, ["-c", str(config), *args], input=input)


class TestTerminalOnlyConfirmation:
    def test_terminal_no_aborts_and_does_nothing(self, lab):
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=True):
            result = run(config, "power", "off", "pi-1", input="n\n")

        assert result.exit_code != 0
        assert "Power off (sbc_name=pi-1). Continue?" in result.output
        power.power_off.assert_not_called()

    def test_terminal_yes_answer_proceeds(self, lab):
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=True):
            result = run(config, "power", "off", "pi-1", input="y\n")

        assert result.exit_code == 0, result.output
        power.power_off.assert_called_once()

    def test_yes_flag_skips_question_on_terminal(self, lab):
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=True):
            result = run(config, "power", "off", "pi-1", "--yes")

        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output
        power.power_off.assert_called_once()

    def test_non_interactive_runs_unchanged(self, lab):
        """Scripts/agents: no question, no --yes needed (no breakage)."""
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=False):
            result = run(config, "power", "off", "pi-1")

        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output
        power.power_off.assert_called_once()


class TestDryRun:
    def test_power_cycle_dry_run(self, lab):
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=True):
            result = run(config, "power", "cycle", "pi-1", "--dry-run")

        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output  # dry run never asks
        assert "Dry run: would power-cycle pi-1 via tasmota plug 10.0.0.9" in (
            result.output
        )
        assert "Nothing changed." in result.output
        power.power_off.assert_not_called()
        power.power_on.assert_not_called()

    def test_sdwire_flash_dry_run(self, lab):
        config, power, tmp_path = lab
        image = tmp_path / "os.img.xz"
        image.write_bytes(b"\0" * 1234)
        ctrl = MagicMock()
        with patch("labctl.sdwire.controller.SDWireController", return_value=ctrl):
            result = run(config, "sdwire", "flash", "pi-1", str(image), "--dry-run")

        assert result.exit_code == 0, result.output
        assert "overwrite the whole SD card" in result.output
        assert "(1234 bytes)" in result.output
        assert "power-cycle pi-1" in result.output
        ctrl.switch_to_host.assert_not_called()
        ctrl.flash_image.assert_not_called()
        power.power_off.assert_not_called()

    def test_sdwire_flash_dry_run_validates_format(self, lab):
        config, _, tmp_path = lab
        bad = tmp_path / "os.iso"
        bad.write_bytes(b"\0")

        result = run(config, "sdwire", "flash", "pi-1", str(bad), "--dry-run")

        assert result.exit_code != 0
        assert "Unsupported image format" in result.output

    def test_sdwire_update_dry_run(self, lab):
        config, power, tmp_path = lab
        src = tmp_path / "kernel.img"
        src.write_text("k")
        ctrl = MagicMock()
        with patch("labctl.sdwire.controller.SDWireController", return_value=ctrl):
            result = run(
                config,
                "sdwire",
                "update",
                "pi-1",
                "-p",
                "1",
                "-c",
                f"{src}:kernel8.img",
                "-d",
                "old.txt",
                "--dry-run",
            )

        assert result.exit_code == 0, result.output
        assert f"copy {src} -> kernel8.img" in result.output
        assert "delete old.txt" in result.output
        ctrl.switch_to_host.assert_not_called()
        ctrl.update_files.assert_not_called()
        power.power_off.assert_not_called()


def test_every_destructive_cli_command_has_yes():
    """Enforcement: anything classified destructive in docs/OPERATIONS.md
    takes --yes (and so asks on a terminal)."""
    import click

    from tests.unit.test_operations_doc import _cli_commands, _section_rows

    commands = dict(_cli_commands(main))
    rows = _section_rows("CLI commands")
    for name, (cls, _prompts, _notes) in rows.items():
        if cls != "destructive":
            continue
        opts = [o.opts for o in commands[name].params if isinstance(o, click.Option)]
        assert any("--yes" in o for o in opts), name


class TestReviewRound1:
    def test_prompt_shows_zero_valued_arguments(self, lab):
        """`0 in (None, False, ())` is True in Python; delay=0 / channel=0
        must still be shown in what the user is asked to approve."""
        config, power, _ = lab
        with patch("labctl.cli._stdin_is_tty", return_value=True):
            result = run(config, "power", "cycle", "pi-1", "--delay", "0", input="n\n")

        assert "delay=0" in result.output
        power.power_off.assert_not_called()

    def test_flash_dry_run_skips_malformed_copy_like_real_run(self, lab):
        config, _, tmp_path = lab
        image = tmp_path / "os.img"
        image.write_bytes(b"\0")

        result = run(
            config,
            "sdwire",
            "flash",
            "pi-1",
            str(image),
            "-c",
            "config.txt",
            "-c",
            "cmdline.txt:cmdline.txt",
            "--dry-run",
        )

        assert result.exit_code == 0, result.output
        assert "Invalid --copy format 'config.txt', would skip" in result.output
        assert "copy config.txt" not in result.output
        assert "copy cmdline.txt -> cmdline.txt on the boot partition" in result.output
