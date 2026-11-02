"""scripts/check-sdwire-sudo.sh: who can't run the SDWire commands (#47)."""

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-sdwire-sudo.sh"
COMMANDS = ["mount", "umount", "dd", "sync", "partprobe", "parted", "blkid"]

# `sudo -n -u USER -- sudo -n CMD --version`: succeeds when CMD is listed
# (or ALL is) in $FAKE_SUDO_DIR/USER, as sudo would for a NOPASSWD rule.
FAKE_SUDO = """#!/bin/bash
user=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    -u) user="$2"; shift 2 ;;
    --|-n) shift ;;
    sudo) shift ;;
    *) cmd="$1"; break ;;
  esac
done
f="$FAKE_SUDO_DIR/$user"
[ -f "$f" ] && grep -qxE "ALL|$cmd" "$f" && exit 0
echo "sudo: a password is required" >&2
exit 1
"""


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "sudo": FAKE_SUDO,
        # "running as root"; any user except "ghost" exists.
        "id": (
            '#!/bin/bash\n[ "$1" = ghost ] && exit 1\n'
            '[ "$1" = -u ] && echo 0\nexit 0\n'
        ),
    }.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    def run(*users, **allowed_by_user):
        for user, cmds in allowed_by_user.items():
            (allowed / user).write_text("\n".join(cmds) + "\n")
        return subprocess.run(
            ["bash", str(SCRIPT), *users],
            capture_output=True,
            text=True,
            env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_SUDO_DIR": str(allowed)},
            timeout=30,
        )

    return run


def _gaps(stdout):
    return stdout.split("without a password: ")[1].splitlines()[0].split()


def test_all_allowed(run):
    result = run("svc", svc=["ALL"])
    assert result.returncode == 0
    assert "[ok] svc can run the SDWire commands" in result.stdout


def test_reports_the_missing_ones_and_the_rule(run):
    result = run("svc", svc=["mount", "umount", "dd", "sync"])
    assert result.returncode == 0
    assert _gaps(result.stdout) == ["partprobe", "parted", "blkid"]
    rule = re.search(r'echo "([^"]*NOPASSWD[^"]*)"', result.stdout).group(1)
    listed = {p.strip() for p in rule.split("NOPASSWD:", 1)[1].split(",")}
    for cmd in COMMANDS:
        d = "sbin" if cmd in ("partprobe", "parted", "blkid") else "bin"
        assert {f"/usr/{d}/{cmd}", f"/{d}/{cmd}"} <= listed
    assert "mktemp" in result.stdout and "visudo -cf" in result.stdout


def test_nothing_allowed_and_unknown_user(run):
    result = run("nobody", "ghost")
    assert _gaps(result.stdout) == COMMANDS
    assert "[!] no user ghost; skipped" in result.stdout


def test_root_and_empty_users_skipped(run):
    result = run("root", "")
    assert result.returncode == 0
    assert result.stdout == ""


def test_not_root_is_skipped():
    if os.geteuid() == 0:
        pytest.skip("running as root")
    result = subprocess.run(
        ["bash", str(SCRIPT), "svc"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0
    assert "must run as root" in result.stdout


def test_printed_install_command_keeps_existing_rules(run, tmp_path):
    """Run the printed `sudo sh -c '...'` (without sudo, against a scratch
    rules file, with stand-ins for visudo and install)."""
    result = run("svc", svc=[])
    line = next(x for x in result.stdout.splitlines() if "sudo sh -c" in x)
    script = line.strip()[len("sudo sh -c '") : -1]
    rules = tmp_path / "labctl-sdwire"
    rules.write_text("other ALL=(root) NOPASSWD: /usr/bin/dd\n")
    script = script.replace("/etc/sudoers.d/labctl-sdwire", str(rules))
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "visudo").write_text("#!/bin/sh\nexit 0\n")
    # install ... SRC DEST: copy the last two arguments.
    (tools / "install").write_text('#!/bin/bash\ncp "${@: -2:1}" "${@: -1}"\n')
    for t in ("visudo", "install"):
        (tools / t).chmod(0o755)
    subprocess.run(
        ["sh", "-c", script],
        check=True,
        env={"PATH": f"{tools}:/usr/bin:/bin"},
        timeout=30,
    )
    text = rules.read_text()
    assert text.startswith("other ALL=(root) NOPASSWD: /usr/bin/dd\n")
    assert "svc ALL=(root) NOPASSWD: /usr/bin/mount" in text


def test_command_list_matches_the_code():
    """Every `sudo <cmd>` labctl runs is in the script's list, and the
    other way round. Lists starting with "sudo" or with a `*prefix`
    (["sudo"] or []) both count."""
    used = set()
    for path in (ROOT / "src" / "labctl").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.List) or len(node.elts) < 2:
                continue
            head = node.elts[0]
            via_sudo = isinstance(head, ast.Starred) or (
                isinstance(head, ast.Constant) and head.value == "sudo"
            )
            if not via_sudo:
                continue
            words = [
                e.value
                for e in node.elts[1:]
                if isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and not e.value.startswith("-")
            ]
            if words:
                used.add(words[0])
    used -= {"udevadm", "systemctl"}  # granted by install-services.sh itself
    text = SCRIPT.read_text()
    listed = set(text.split('COMMANDS="', 1)[1].split('"', 1)[0].split())
    assert used == listed


def test_called_by_the_install_and_update_scripts():
    for name in ("install-services.sh", "update.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert '"$SCRIPT_DIR/check-sdwire-sudo.sh" labctl "${SUDO_USER:-}"' in text
