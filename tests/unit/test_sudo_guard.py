"""The suite-wide guard against starting a real sudo (#50)."""

import subprocess

import pytest

from tests.conftest import _runs_sudo


@pytest.mark.parametrize(
    "args",
    [
        ["sudo", "dd", "if=/dev/zero"],
        ["/usr/bin/sudo", "-n", "true"],
        ["env", "LC_ALL=C", "sudo", "mount"],
        ["timeout", "5", "sudo", "sync"],
        "sudo udevadm control --reload-rules",
    ],
)
def test_sudo_refused(args):
    assert _runs_sudo(args)
    with pytest.raises(RuntimeError, match="real sudo"):
        subprocess.run(args, shell=isinstance(args, str), check=False)


@pytest.mark.parametrize(
    "args",
    [["true"], ["env", "LC_ALL=C", "true"], ["echo", "sudo"], "echo sudo"],
)
def test_other_commands_run(args):
    assert not _runs_sudo(args)
    assert subprocess.run(args, shell=isinstance(args, str)).returncode == 0


def test_mocked_sudo_calls_are_unaffected(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    subprocess.run(["sudo", "sync"])
    assert calls == [(["sudo", "sync"],)]
