"""scripts/update.sh pieces that run on production deploys: its embedded
Python must keep matching labctl's API, and its MCP startup check must
report what the restarted service will actually do."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parents[2] / "scripts" / "update.sh"
LABCTL = shutil.which("labctl", path=str(Path(sys.executable).parent))


def _snippet(marker: str) -> str:
    """The heredoc body that follows the line containing ``marker``."""
    text = UPDATE_SH.read_text()
    match = re.search(
        re.escape(marker) + r"[^\n]*<<'PY'[^\n]*\n(.*?)\nPY\n", text, flags=re.S
    )
    assert match, f"no PY heredoc after {marker!r} in update.sh"
    return match.group(1)


def _run(snippet: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-", *args],
        input=snippet,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def write_config(tmp_path):
    def write(body: str, name: str = "config.yaml") -> Path:
        path = tmp_path / name
        path.write_text(f"database_path: {tmp_path / 'x.db'}\n{body}")
        return path

    return write


class TestAllowlistCheck:
    """Step 3b: exit status says whether an allowlist key is set."""

    SNIPPET_MARKER = 'if ! "$LABCTL_VENV/bin/python" - "$cfg" "$key"'

    def test_set_and_missing(self, write_config):
        config = write_config("mcp:\n  allowed_read_paths: [/var/lib/labctl/images]\n")
        snippet = _snippet(self.SNIPPET_MARKER)
        assert _run(snippet, str(config), "allowed_read_paths").returncode == 0
        assert _run(snippet, str(config), "allowed_write_paths").returncode == 1


# ---------------------------------------------------------------------------
# Step 3c: run the unit's own command line with --check
# ---------------------------------------------------------------------------


def _step_3c() -> str:
    text = UPDATE_SH.read_text()
    start = text.index("# 3c. MCP HTTP authentication")
    end = text.index("# 4. Verify install")
    return text[start:end]


EXEC_RECORD = (
    "{{ path={argv0} ; argv[]={argv} ; ignore_errors=no ; "
    "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}"
)


@pytest.fixture
def run_step(tmp_path):
    """Run step 3c with stub `systemctl` (unit properties given per test)
    and `runuser` (runs the command as the current user)."""
    if LABCTL is None:
        pytest.skip("labctl console script not installed next to the interpreter")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "runuser").write_text(
        '#!/bin/bash\n# runuser -u USER -- CMD...\nshift 3\nexec "$@"\n'
    )
    (stubs / "runuser").chmod(0o755)

    def run(
        argv: str,
        environment: str = "",
        env_files: str = "",
        workdir=None,
        exe=None,
        caller_env=None,
    ):
        props = {
            "ExecStart": EXEC_RECORD.format(argv0=exe or argv.split()[0], argv=argv),
            "User": os.environ.get("USER", "root"),
            "WorkingDirectory": str(workdir or tmp_path),
            "Environment": environment,
            "EnvironmentFiles": env_files,
        }
        lines = ["#!/bin/bash", 'case "$1" in is-enabled) exit 0 ;; esac']
        lines.append('case "$3" in')
        for key, value in props.items():
            lines.append(f"  {key}) cat <<'EOF'\n{value}\nEOF\n  ;;")
        lines.append("esac")
        (stubs / "systemctl").write_text("\n".join(lines) + "\n")
        (stubs / "systemctl").chmod(0o755)
        script = "set -e\n" + _step_3c()
        env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
        env.pop("LABCTL_CONFIG", None)
        env.pop("LABCTL_CONFIG_EXCLUSIVE", None)
        env["MCP_CHECK_TIMEOUT"] = "10"
        env.update(caller_env or {})
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    return run


AUTH = (
    "auth:\n  enabled: true\n  users:\n"
    "    - username: a\n      api_key: kkkkkkkkkkkkkkkkkkkkkkkk\n"
)


class TestMcpUnitArgv:
    def test_extracts_argv(self):
        fn = re.search(r"mcp_unit_argv\(\) \{.*?\n\}", UPDATE_SH.read_text(), re.S)
        argv = "/opt/labctl/venv/bin/labctl -c /etc/labctl/config.yaml mcp --http 8080"
        record = EXEC_RECORD.format(argv0=argv.split()[0], argv=argv)
        out = subprocess.run(
            ["bash", "-c", fn.group(0) + "\nmcp_unit_argv"],
            input=record,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert out == argv

    def test_at_prefix_uses_path_as_executable(self):
        """`ExecStart=@/path argv0 ...`: argv[0] is only a display name."""
        fn = re.search(r"mcp_unit_argv\(\) \{.*?\n\}", UPDATE_SH.read_text(), re.S)
        record = EXEC_RECORD.format(
            argv0="/opt/labctl/venv/bin/labctl", argv="labctl-mcp mcp --http 8080"
        )
        out = subprocess.run(
            ["bash", "-c", fn.group(0) + "\nmcp_unit_argv"],
            input=record,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert out == "/opt/labctl/venv/bin/labctl mcp --http 8080"


class TestMcpStartupCheck:
    def test_no_auth_loopback(self, run_step, write_config):
        cfg = write_config("")
        out = run_step(f"{LABCTL} -c {cfg} mcp --http 8080")
        assert "[ok] labctl-mcp startup check:" in out
        assert "auth: none" in out
        assert f"config: {cfg}" in out

    def test_auth_required(self, run_step, write_config):
        out = run_step(f"{LABCTL} -c {write_config(AUTH)} mcp --http 8080")
        assert "[!!] labctl-mcp requires API keys" in out
        assert "auth: required" in out

    def test_refused_non_loopback(self, run_step, write_config):
        out = run_step(f"{LABCTL} -c {write_config('')} mcp --http 8080 --host 0.0.0.0")
        assert "[!!] labctl-mcp startup check failed" in out
        assert "without authentication" in out

    def test_missing_config_file_refused(self, run_step, tmp_path):
        """labctl -c rejects a missing file: the service would fail too."""
        out = run_step(f"{LABCTL} -c {tmp_path / 'nope.yaml'} mcp --http 8080")
        assert "[!!] labctl-mcp startup check failed" in out
        assert "does not exist" in out

    def test_relative_config_resolved_in_workdir(self, run_step, write_config):
        """`-c mcp.yaml` is relative to the unit's WorkingDirectory."""
        cfg = write_config(AUTH, name="mcp.yaml")
        out = run_step(f"{LABCTL} -c mcp.yaml mcp --http 8080", workdir=cfg.parent)
        assert "auth: required" in out

    def test_config_from_unit_environment(self, run_step, write_config):
        cfg = write_config(AUTH, name="env.yaml")
        out = run_step(
            f"{LABCTL} mcp --http 8080",
            environment=f"LABCTL_CONFIG={cfg} LABCTL_CONFIG_EXCLUSIVE=1",
        )
        assert "auth: required" in out
        assert f"config: {cfg}" in out

    def test_stdio_unit(self, run_step, write_config):
        out = run_step(f"{LABCTL} -c {write_config('')} mcp")
        assert "auth: n/a" in out

    def test_variable_substitution_not_guessed(self, run_step):
        out = run_step(f"{LABCTL} mcp --http 8080 --host ${{MCP_BIND}}")
        assert "Could not check" in out

    def test_environment_file_noted(self, run_step, write_config):
        out = run_step(
            f"{LABCTL} -c {write_config('')} mcp --http 8080",
            env_files="/etc/default/labctl-mcp (ignore_errors=no)",
        )
        assert "EnvironmentFile= was not applied" in out

    def test_broken_config_is_not_ok(self, run_step, tmp_path):
        """A config that exists but doesn't parse would start the server on
        built-in defaults: flagged, not "[ok]"."""
        cfg = tmp_path / "broken.yaml"
        cfg.write_text("auth: [unclosed\n")
        out = run_step(f"{LABCTL} -c {cfg} mcp --http 8080")
        assert "[!!] labctl-mcp startup check failed" in out
        assert "config not loaded" in out

    def test_warnings_are_not_ok(self, run_step, write_config):
        cfg = write_config("mcp:\n  confirm_exempt: [serial_sen]\n")
        out = run_step(f"{LABCTL} -c {cfg} mcp --http 8080")
        assert "[!!] labctl-mcp startup check passed with warnings" in out
        assert "serial_sen" in out

    def test_caller_environment_does_not_leak(self, run_step, tmp_path):
        """systemd starts the service with a clean environment; so does the
        check, whatever the admin's shell exports. (Unit without -c, so the
        config comes from the environment's search path.)"""
        xdg = tmp_path / "xdg"
        (xdg / "labctl").mkdir(parents=True)
        unit_cfg = xdg / "labctl" / "config.yaml"
        unit_cfg.write_text(f"database_path: {tmp_path / 'x.db'}\n")
        dev = tmp_path / "dev.yaml"
        dev.write_text(f"database_path: {tmp_path / 'x.db'}\n{AUTH}")
        out = run_step(
            f"{LABCTL} mcp --http 8080",
            environment=f"XDG_CONFIG_HOME={xdg}",
            caller_env={"LABCTL_CONFIG": str(dev), "LABCTL_CONFIG_EXCLUSIVE": "1"},
        )
        assert "auth: none" in out
        assert f"config: {unit_cfg}" in out

    def test_missing_ok_workdir_prefix(self, run_step, write_config):
        """`WorkingDirectory=-PATH` shows as "!PATH"."""
        cfg = write_config(AUTH, name="mcp.yaml")
        out = run_step(
            f"{LABCTL} -c mcp.yaml mcp --http 8080", workdir=f"!{cfg.parent}"
        )
        assert "auth: required" in out

    def test_command_without_check_support(self, run_step, tmp_path):
        """A drop-in command that ignores --check and keeps running is
        reported as uncheckable, not as a refusal, and doesn't hang."""
        server = tmp_path / "serve"
        server.write_text("#!/bin/sh\nexec sleep 600\n")
        server.chmod(0o755)
        out = run_step(f"{server} --http 8080")
        assert "doesn't support --check" in out
