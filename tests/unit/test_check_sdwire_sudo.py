"""scripts/check-sdwire-sudo.sh: who can't run the SDWire commands (#47)."""

import ast
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-sdwire-sudo.sh"
COMMANDS = ["mount", "umount", "dd", "sync", "partprobe", "parted", "blkid"]
# What real `sudo -l -U` prints before the rules.
HEADER = "User svc may run the following commands on host:\n"

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
    (bindir / "id").write_text("#!/bin/bash\necho 0\n")  # "running as root"
    (bindir / "id").chmod(0o755)
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
    result = run("svc", svc=HEADER + "    (ALL) NOPASSWD: ALL\n")
    assert result.returncode == 0
    assert "[ok] svc can run the SDWire commands" in result.stdout


def test_reports_the_missing_ones_and_the_rule(run):
    listing = HEADER + (
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
    assert "visudo -cf /tmp/labctl-sdwire.new && sudo install -m 440" in (result.stdout)


def test_a_password_rule_does_not_count(run):
    """The services have no terminal: only NOPASSWD entries help."""
    listing = HEADER + "    (root) /usr/bin/mount, /usr/bin/dd\n"
    result = run("svc", svc=listing)
    assert "without a password: mount umount dd sync" in result.stdout


def test_no_rules_at_all(run):
    result = run("nobody")
    assert result.returncode == 0
    assert "without a password: " + " ".join(COMMANDS) in result.stdout


def test_a_command_name_inside_another_path_is_not_a_match(run):
    listing = HEADER + "    (root) NOPASSWD: /usr/local/bin/mydd, /opt/blkid-wrapper\n"
    result = run("svc", svc=listing)
    assert "dd" in result.stdout.split("without a password: ")[1].split()


@pytest.mark.parametrize(
    "listing, missing",
    [
        # ALL with a negation of something else: everything allowed.
        ("    (ALL : ALL) NOPASSWD: ALL, !/bin/su\n", ""),
        # ALL, but dd negated: the last match wins.
        ("    (ALL) NOPASSWD: ALL, !/usr/bin/dd\n", "dd"),
        # A tag change mid-line: dd and mount need a password.
        (
            "    (root) NOPASSWD: /usr/bin/udevadm, PASSWD: /usr/bin/dd, "
            "/usr/bin/mount\n",
            "mount umount dd sync",
        ),
        # Another runas user doesn't count.
        ("    (nobody) NOPASSWD: ALL\n", "mount umount dd sync"),
        # A command restricted to arguments doesn't count.
        ("    (root) NOPASSWD: /usr/bin/dd if=/dev/zero\n", "mount umount dd"),
        # A wrapped entry: the continuation line counts.
        (
            "    (root) NOPASSWD: /usr/bin/mount, /usr/bin/umount, /usr/bin/dd,\n"
            "        /usr/bin/sync, /usr/sbin/partprobe, /usr/sbin/parted,\n"
            "        /usr/sbin/blkid\n",
            "",
        ),
    ],
)
def test_rule_semantics(run, listing, missing):
    result = run("svc", svc=HEADER + listing)
    if missing:
        gaps = result.stdout.split("without a password: ")[1].splitlines()[0]
        assert gaps.split()[: len(missing.split())] == missing.split()
    else:
        assert "[ok] svc" in result.stdout


def test_defaults_not_authenticate_counts_as_passwordless(run):
    listing = (
        "Matching Defaults entries for svc on host:\n"
        "    env_reset, !authenticate\n\n" + HEADER + "    (root) /usr/bin/mount, "
        "/usr/bin/umount, /usr/bin/dd, /usr/bin/sync, /usr/sbin/partprobe, "
        "/usr/sbin/parted, /usr/sbin/blkid\n"
    )
    assert "[ok] svc" in run("svc", svc=listing).stdout


def test_not_root_is_skipped(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "svc"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert result.returncode == 0
    assert "must run as root" in result.stdout


def test_command_list_matches_the_code():
    """Every `sudo <cmd>` labctl runs is in the script's list, and the
    other way round (#47)."""
    used = set()
    for path in (ROOT / "src" / "labctl").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.List) and len(node.elts) >= 2:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and first.value == "sudo":
                    rest = [
                        e.value
                        for e in node.elts[1:]
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    words = [w for w in rest if not w.startswith("-")]
                    if words:
                        used.add(words[0])
    used -= {"udevadm", "systemctl"}  # granted by install-services.sh itself
    text = SCRIPT.read_text()
    listed = set(text.split('COMMANDS="', 1)[1].split('"', 1)[0].split())
    assert used == listed


def test_root_and_empty_users_skipped(run):
    result = run("root", "")
    assert result.returncode == 0
    assert result.stdout == ""


def test_called_by_the_install_and_update_scripts():
    for name in ("install-services.sh", "update.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert '"$SCRIPT_DIR/check-sdwire-sudo.sh" labctl "${SUDO_USER:-}"' in text
