"""scripts/check-sdwire-sudo.sh: who can't run the SDWire commands (#47)."""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-sdwire-sudo.sh"
COMMANDS = ["mount", "umount", "dd", "sync", "partprobe", "parted", "blkid"]

# `runuser -u USER -- CMD...`: run CMD as USER (here: tell the fake sudo).
FAKE_RUNUSER = """#!/bin/bash
[ "$1" = -u ] && [ "$3" = -- ] || exit 2
export FAKE_USER="$2"
shift 3
exec "$@"
"""

# `sudo -n -k CMD --version`: succeeds when CMD is listed (or ALL is) in
# $FAKE_SUDO_DIR/$FAKE_USER, as sudo would for a NOPASSWD rule. Anything
# else fails: without -k a cached login could make a real sudo succeed with
# no rule, without -n it could ask for a password, and without --version
# the command would really run (dd copying stdin, parted interactive).
FAKE_SUDO = """#!/bin/bash
n=""; k=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n) n=1; shift ;;
    -k) k=1; shift ;;
    *) cmd="$1"; break ;;
  esac
done
[ -n "$n" ] && [ -n "$k" ] && [ "$#" -eq 2 ] && [ "$2" = --version ] \
  || { echo "unexpected probe: $*" >&2; exit 3; }
f="$FAKE_SUDO_DIR/$FAKE_USER"
[ -f "$f" ] && grep -qxE "ALL|$cmd" "$f" && exit 0
echo "sudo: a password is required" >&2
exit 1
"""

# `timeout 10 CMD...`: runs CMD, or "times out" (124) when CMD's words
# include $FAKE_SLOW.
FAKE_TIMEOUT = """#!/bin/bash
[ "$1" = 10 ] || exit 2
shift
for w in "$@"; do [ -n "${FAKE_SLOW:-}" ] && [ "$w" = "$FAKE_SLOW" ] && exit 124; done
exec "$@"
"""

USUAL_PATHS = (
    "/usr/bin/mount /usr/bin/umount /usr/bin/dd /usr/bin/sync "
    "/usr/sbin/partprobe /usr/sbin/parted /usr/sbin/blkid"
)


def _rule_for(user, installed_paths):
    """The script's rule_for, run on its own."""
    text = SCRIPT.read_text()
    func = text[text.index("rule_for() {") :]
    func = func[: func.index("\n}\n") + 3]
    return subprocess.run(
        [shutil.which("bash"), "-c", f'{func}\nrule_for "$1"', "-", user],
        capture_output=True,
        text=True,
        check=True,
        env={"INSTALLED_PATHS": installed_paths},
        timeout=30,
    ).stdout.strip()


@pytest.fixture
def run(tmp_path):
    """Run the script with fake runuser/sudo/id, stand-ins for the SDWire
    commands (all installed unless named in `not_installed`), and nothing
    else on PATH but bash and grep."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "grep"):
        (bindir / tool).symlink_to(shutil.which(tool))
    for name, body in {
        "runuser": FAKE_RUNUSER,
        "sudo": FAKE_SUDO,
        "timeout": FAKE_TIMEOUT,
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

    def run(*users, not_installed=(), slow="", **allowed_by_user):
        for cmd in COMMANDS:
            stub = bindir / cmd
            if cmd in not_installed:
                stub.unlink(missing_ok=True)
            else:
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
        for user, cmds in allowed_by_user.items():
            (allowed / user).write_text("\n".join(cmds) + "\n")
        return subprocess.run(
            [shutil.which("bash"), str(SCRIPT), *users],
            capture_output=True,
            text=True,
            env={
                "PATH": str(bindir),
                "FAKE_SUDO_DIR": str(allowed),
                "FAKE_SLOW": slow,
            },
            timeout=30,
        )

    return run


def _gaps(stdout):
    return stdout.split("without a password: ")[1].splitlines()[0].split(", ")


def test_all_allowed(run):
    result = run("svc", svc=["ALL"])
    assert result.returncode == 0
    assert "[ok] svc can run the SDWire commands" in result.stdout


def test_reports_the_missing_ones_and_the_rule(run):
    result = run("svc", svc=["mount", "umount", "dd", "sync"])
    assert result.returncode == 0
    assert _gaps(result.stdout) == ["partprobe", "parted", "blkid"]
    rule = re.search(r'echo "([^"]*NOPASSWD[^"]*)"', result.stdout).group(1)
    listed = [p.strip() for p in rule.split("NOPASSWD:", 1)[1].split(",")]
    # The paths found (the stand-ins here).
    assert [Path(p).name for p in listed] == COMMANDS
    assert "mktemp" in result.stdout and "visudo -cf" in result.stdout


def test_nothing_allowed_and_unknown_user(run):
    result = run("nobody", "ghost")
    assert _gaps(result.stdout) == COMMANDS
    assert "[!] no user ghost; skipped" in result.stdout


@pytest.mark.parametrize("admin", ["", "root"])
def test_root_shell_says_how_to_check_yours(run, admin):
    """update.sh run from a root shell passes an empty ${SUDO_USER:-}, or
    root (`sudo` from a root shell)."""
    result = run("svc", admin, svc=["ALL"])
    assert result.returncode == 0
    assert "[ok] svc" in result.stdout
    assert "run from a root shell" in result.stdout
    assert "YOUR_USER" in result.stdout


def test_each_user_checked_once(run):
    """The admin can be the service user (labctl labctl)."""
    result = run("svc", "svc", svc=[])
    assert result.stdout.count("[!] svc can't run") == 1


def test_timed_out_probe_is_named(run):
    result = run("svc", slow="dd", svc=["mount", "umount"])
    assert _gaps(result.stdout) == [
        "timed out: dd",
        "sync",
        "partprobe",
        "parted",
        "blkid",
    ]


def test_no_sudo_is_reported(run, tmp_path):
    (tmp_path / "bin" / "sudo").unlink()
    result = run("svc")
    assert result.returncode == 0
    assert "sudo is not installed" in result.stdout
    assert "can't run" not in result.stdout


def test_rule_covers_the_path_found():
    rule = _rule_for("u", "/usr/bin/dd /usr/local/sbin/parted /usr/sbin/blkid")
    assert rule == (
        "u ALL=(root) NOPASSWD: /usr/bin/dd, /bin/dd, "
        "/usr/local/sbin/parted, /usr/sbin/blkid, /sbin/blkid"
    )


def test_not_installed_is_not_a_missing_rule(run):
    result = run("svc", not_installed=["parted"], svc=["mount", "umount", "dd"])
    assert "not installed: parted" in result.stdout
    assert _gaps(result.stdout) == ["sync", "partprobe", "blkid"]


def test_no_runuser_is_skipped(run, tmp_path):
    (tmp_path / "bin" / "runuser").unlink()
    result = run("svc")
    assert result.returncode == 0
    assert "needs runuser" in result.stdout


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


def _printed_install(stdout):
    line = next(x for x in stdout.splitlines() if "sudo sh -c" in x)
    return line.strip()


def _run_install(run, tmp_path, visudo_ok):
    """Run the printed `sudo sh -c '...'` (without sudo, against a scratch
    rules file, with stand-ins for visudo and install)."""
    script = _printed_install(run("svc", svc=[]).stdout)[len("sudo sh -c '") : -1]
    rules = tmp_path / "labctl-sdwire"
    rules.write_text("other ALL=(root) NOPASSWD: /usr/bin/dd\n")
    script = script.replace("/etc/sudoers.d/labctl-sdwire", str(rules))
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "visudo").write_text(f"#!/bin/sh\nexit {0 if visudo_ok else 1}\n")
    # install ... SRC DEST: copy the last two arguments.
    (tools / "install").write_text('#!/bin/bash\ncp "${@: -2:1}" "${@: -1}"\n')
    for t in ("visudo", "install"):
        (tools / t).chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", script],
        env={"PATH": f"{tools}:/usr/bin:/bin"},
        timeout=30,
    )
    return result.returncode, rules.read_text()


def test_printed_install_command_keeps_existing_rules(run, tmp_path):
    status, text = _run_install(run, tmp_path, visudo_ok=True)
    assert status == 0
    assert text.startswith("other ALL=(root) NOPASSWD: /usr/bin/dd\n")
    assert text.splitlines()[1].startswith("svc ALL=(root) NOPASSWD: ")
    assert text.splitlines()[1].endswith("/bin/blkid")


def test_printed_install_command_fails_when_visudo_does(run, tmp_path):
    status, text = _run_install(run, tmp_path, visudo_ok=False)
    assert status != 0
    assert text == "other ALL=(root) NOPASSWD: /usr/bin/dd\n"


def test_readme_shows_the_printed_install_command(run):
    """README's one-liner is what the script prints where the commands are
    at their usual paths."""
    printed = _printed_install(run("<user>", **{"<user>": []}).stdout)
    here = re.search(r'echo "([^"]*NOPASSWD[^"]*)"', printed).group(1)
    printed = printed.replace(here, _rule_for("<user>", USUAL_PATHS))
    readme = (ROOT / "README.md").read_text()
    assert printed in readme.splitlines()


def test_command_list_matches_the_code():
    """Every `sudo <cmd>` labctl runs is in the script's list, and the
    other way round. Lists or tuples starting with "sudo" or with
    `*prefix` (["sudo"] or []) both count."""
    used = set()
    for path in (ROOT / "src" / "labctl").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
                continue
            head = node.elts[0]
            via_sudo = (
                isinstance(head, ast.Starred)
                and isinstance(head.value, ast.Name)
                and head.value.id == "prefix"
            ) or (isinstance(head, ast.Constant) and head.value == "sudo")
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
