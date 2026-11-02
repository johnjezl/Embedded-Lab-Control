"""The suite-wide guard against starting a real sudo (#50)."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import _runs_sudo


@pytest.mark.parametrize(
    "args, kw",
    [
        (["sudo", "dd", "if=/dev/zero"], {}),
        (["/usr/bin/sudo", "-n", "true"], {}),
        (["env", "LC_ALL=C", "sudo", "mount"], {}),
        (["env", "-u", "X", "sudo", "x"], {}),
        (["timeout", "5s", "sudo", "sync"], {}),
        (["timeout", "--signal", "KILL", "5", "sudo", "sync"], {}),
        (["sh", "-c", "sudo x"], {}),
        ("sudo udevadm control --reload-rules", {"shell": True}),
        ("cd / && sudo x", {"shell": True}),
        ("exec sudo x", {"shell": True}),
        (["sudo dd if=/dev/zero"], {"shell": True}),
        (["x", "dd"], {"executable": "/usr/bin/sudo"}),
    ],
)
def test_sudo_detected(args, kw):
    assert _runs_sudo(args, **kw)


@pytest.mark.parametrize(
    "args, kw",
    [
        (["true"], {}),
        (["env", "LC_ALL=C", "true"], {}),
        (["echo", "sudo"], {}),
        (Path("/bin/true"), {}),
        ("echo ok", {"shell": True}),
    ],
)
def test_other_commands_not_detected(args, kw):
    assert not _runs_sudo(args, **kw)


def test_a_real_run_is_a_test_failure_not_an_exception():
    """The refusal can't be caught by the code under test (it catches
    RuntimeError and Exception around its sudo calls)."""
    with pytest.raises(pytest.fail.Exception, match="real sudo"):
        try:
            subprocess.run(["sudo", "-n", "true"], check=False)
        except Exception:  # noqa: BLE001 - what labctl code does
            pass


def test_os_system_is_guarded():
    with pytest.raises(pytest.fail.Exception, match="real sudo"):
        os.system("sudo -n true")


def test_path_executable_still_runs():
    assert subprocess.run(Path("/bin/true")).returncode == 0


def test_guard_sits_below_mocks_of_the_process_start():
    """With the process start mocked, a non-sudo command is built normally
    and a sudo one is still refused."""
    with patch.object(subprocess.Popen, "_execute_child", MagicMock()):
        proc = subprocess.Popen(["true"])
        assert proc.args == ["true"]
        with pytest.raises(pytest.fail.Exception):
            subprocess.Popen(["sudo", "sync"])


def test_module_level_mock_of_run_is_honoured(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    subprocess.run(["sudo", "sync"])
    assert calls == [(["sudo", "sync"],)]


@pytest.mark.allow_sudo
def test_allow_sudo_marker_lets_it_through():
    with patch.object(subprocess.Popen, "_execute_child", MagicMock()):
        subprocess.Popen(["sudo", "sync"])  # not refused (and not started)
