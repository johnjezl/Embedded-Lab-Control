"""scripts/check-sdwire-sudo.sh: who can't run the SDWire commands (#47)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-sdwire-sudo.sh"
COMMANDS = ["mount", "umount", "dd", "sync", "partprobe", "parted", "blkid"]

FAKE_SUDO = """#!/bin/bash
# `sudo -n -l -U USER`: print $FAKE_SUDO_DIR/USER, or fail (no rules).
user="${@: -1}"
[ -f "$FAKE_SUDO_DIR/$user" ] && cat "$FAKE_SUDO_DIR/$user" && exit 0
exit 1
"""


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sudo").write_text(FAKE_SUDO)
    (bindir / "sudo").chmod(0o755)
    listings = tmp_path / "listings"
    listings.mkdir()

    def run(*users, **listing_by_user):
        for user, text in listing_by_user.items():
            (listings / user).write_text(text)
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "FAKE_SUDO_DIR": str(listings),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *users],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    return run


def test_all_allowed(run):
    result = run("svc", svc="    (ALL) NOPASSWD: ALL\n")
    assert result.returncode == 0
    assert "[ok] svc can run the SDWire commands" in result.stdout


def test_reports_the_missing_ones_and_the_rule(run):
    listing = (
        "    (ALL) NOPASSWD: /usr/bin/udevadm\n"
        "    (root) NOPASSWD: /usr/bin/mount, /usr/bin/umount, /bin/dd, "
        "/usr/bin/dd, /usr/bin/sync, /bin/sync\n"
    )
    result = run("svc", svc=listing)
    assert result.returncode == 0
    assert "without a password: partprobe parted blkid" in result.stdout
    rule = next(line for line in result.stdout.splitlines() if "NOPASSWD" in line)
    paths = rule.split("NOPASSWD:", 1)[1].split("'")[0]
    listed = {p.strip() for p in paths.split(",")}
    for cmd in COMMANDS:
        d = "sbin" if cmd in ("partprobe", "parted", "blkid") else "bin"
        assert {f"/usr/{d}/{cmd}", f"/{d}/{cmd}"} <= listed
    assert "/etc/sudoers.d/labctl-sdwire" in result.stdout


def test_a_password_rule_does_not_count(run):
    """The services have no terminal: only NOPASSWD entries help."""
    listing = "    (root) /usr/bin/mount, /usr/bin/dd\n"
    result = run("svc", svc=listing)
    assert "without a password: mount umount dd sync" in result.stdout


def test_no_rules_at_all(run):
    result = run("nobody")
    assert result.returncode == 0
    assert "without a password: " + " ".join(COMMANDS) in result.stdout


def test_a_command_name_inside_another_path_is_not_a_match(run):
    listing = "    (root) NOPASSWD: /usr/local/bin/mydd, /opt/blkid-wrapper\n"
    result = run("svc", svc=listing)
    assert "dd" in result.stdout.split("without a password: ")[1].split()


def test_root_and_empty_users_skipped(run):
    result = run("root", "")
    assert result.returncode == 0
    assert result.stdout == ""


def test_called_by_the_install_and_update_scripts():
    for name in ("install-services.sh", "update.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert '"$SCRIPT_DIR/check-sdwire-sudo.sh" labctl "${SUDO_USER:-}"' in text
