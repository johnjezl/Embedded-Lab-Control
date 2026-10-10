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
# Steps 5-6: restart, then report each service from its own journal
# ---------------------------------------------------------------------------


def _steps_5_6() -> str:
    text = UPDATE_SH.read_text()
    return text[text.index("# 5. Restart services") :]


@pytest.fixture
def run_restart(tmp_path):
    """Run steps 5-6 with stub systemctl/journalctl/ss/sleep. ``active``
    maps service -> running?; ``logs`` maps service -> journal text;
    ``listening`` says whether labctl-mcp's main process (pid 4242) has a
    listening socket."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "journalctl.calls"

    def run(
        active: dict,
        logs: dict,
        listening: bool = True,
        listener_pid: int = 4242,
        cgroup_pids=(4242,),
        ss_present: bool = True,
    ):
        systemctl = ["#!/bin/bash", 'case "$1" in']
        systemctl.append("  is-enabled) exit 0 ;;")
        systemctl.append("  restart) exit 0 ;;")
        systemctl.append(
            '  show) case "$3" in MainPID) echo 4242 ;;'
            " ControlGroup) echo /system.slice/labctl-mcp.service ;; esac ;;"
        )
        running = " ".join(name for name, up in active.items() if up)
        systemctl.append(
            f'  is-active) for s in {running}; do [ "$3" = "$s" ] && exit 0; done;'
            " exit 3 ;;"
        )
        systemctl.append("esac")
        (stubs / "systemctl").write_text("\n".join(systemctl) + "\n")
        journal = [
            "#!/bin/bash",
            f'echo "$*" >> {calls}',
            'if [[ "$*" == *--show-cursor* ]]; then echo "-- cursor: s=c1"; exit 0; fi',
            'case "$2" in',
        ]
        for name, text in logs.items():
            journal.append(f"  {name}) cat <<'EOF'\n{text}\nEOF\n  ;;")
        journal.append("esac")
        (stubs / "journalctl").write_text("\n".join(journal) + "\n")
        ss_line = (
            "LISTEN 0 2048 127.0.0.1:8080 0.0.0.0:* "
            f'users:(("labctl",pid={listener_pid},fd=6))'
            if listening
            else 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=42420,fd=3))'
        )
        cgroup = tmp_path / "cgroup" / "system.slice" / "labctl-mcp.service"
        cgroup.mkdir(parents=True, exist_ok=True)
        (cgroup / "cgroup.procs").write_text("".join(f"{p}\n" for p in cgroup_pids))
        (stubs / "ss").write_text(f"#!/bin/sh\necho '{ss_line}'\n")
        (stubs / "sleep").write_text("#!/bin/sh\n")
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "CGROUP_ROOT": str(tmp_path / "cgroup"),
            "SS_CMD": "ss" if ss_present else "no-such-ss-command",
        }
        result = subprocess.run(
            ["bash", "-c", "set -e\n" + _steps_5_6()],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return result, calls.read_text() if calls.exists() else ""

    return run


ALL_UP = {"labctl-web": True, "labctl-monitor": True, "labctl-mcp": True}


class TestRestartReport:
    def test_mcp_auth_mode_shown_from_this_restart(self, run_restart):
        result, calls = run_restart(
            ALL_UP,
            {
                "labctl-mcp": "Starting MCP server (HTTP on 127.0.0.1:8080)...\n"
                "MCP HTTP: API keys required"
            },
        )
        assert result.returncode == 0, result.stderr
        assert "[ok] labctl-mcp running" in result.stdout
        assert "     MCP HTTP: API keys required" in result.stdout
        assert "MCP HTTP clients must send" in result.stdout
        assert "'Authorization: Bearer <api_key>'" in result.stdout
        # Only this restart's lines, not an earlier run's: after a cursor.
        unit_calls = [c for c in calls.splitlines() if c.startswith("-u ")]
        assert unit_calls
        assert all("--after-cursor s=c1" in c for c in unit_calls)

    def test_no_auth_heads_up_without_keys(self, run_restart):
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": "MCP HTTP: no authentication (loopback only)"}
        )
        assert result.returncode == 0, result.stderr
        assert "MCP HTTP: no authentication" in result.stdout
        assert "Authorization: Bearer" not in result.stdout

    def test_failed_service_shows_its_log_and_fails(self, run_restart):
        result, _ = run_restart(
            {**ALL_UP, "labctl-mcp": False},
            {"labctl-mcp": "Error: Refusing to serve MCP over HTTP on 0.0.0.0"},
            listening=False,
        )
        assert result.returncode == 1
        assert "[!!] labctl-mcp FAILED" in result.stdout
        assert "     Error: Refusing to serve MCP over HTTP" in result.stdout
        assert "Some services failed to start: labctl-mcp" in result.stdout

    def test_auth_line_found_beyond_displayed_tail(self, run_restart):
        """Request logs after startup must not hide the heads-up."""
        noise = "\n".join(f'INFO: 127.0.0.1 - "POST /mcp" 401 #{i}' for i in range(40))
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": f"MCP HTTP: API keys required\n{noise}"}
        )
        assert result.returncode == 0, result.stderr
        assert "     MCP HTTP: API keys required" not in result.stdout  # tail only
        assert "must send" in result.stdout

    def test_missing_auth_line_flagged(self, run_restart):
        """A slow or silent start isn't reported as a clean one."""
        result, _ = run_restart(ALL_UP, {"labctl-mcp": "Started labctl-mcp."})
        assert result.returncode == 0, result.stderr
        assert "hasn't logged its auth mode yet" in result.stdout

    def test_active_but_not_listening_is_failed(self, run_restart):
        """Checked on the socket, not on log wording: a server whose bind
        failed (still exiting, so systemd says active) or that never binds
        is FAILED, whatever its log says."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: no authentication (loopback only)"},
            listening=False,
        )
        assert result.returncode == 1
        assert "[!!] labctl-mcp FAILED" in result.stdout
        assert "Some services failed to start: labctl-mcp" in result.stdout

    def test_other_process_listening_does_not_count(self, run_restart):
        """Only labctl-mcp's own main process counts (pid 4242 vs 42420)."""
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": "MCP HTTP: API keys required"}, listening=False
        )
        assert "[!!] labctl-mcp FAILED" in result.stdout

    def test_wrapper_child_listening_counts(self, run_restart):
        """`ExecStart=sh -c '...'`: MainPID is the shell, the server is a
        child in the unit's cgroup; its socket counts."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            listener_pid=4243,
            cgroup_pids=(4242, 4243),
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-mcp running" in result.stdout

    def test_process_outside_unit_does_not_count(self, run_restart):
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            listener_pid=5555,
            cgroup_pids=(4242, 4243),
        )
        assert "[!!] labctl-mcp FAILED" in result.stdout

    def test_without_ss_not_reported_failed(self, run_restart):
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": "MCP HTTP: API keys required"}, ss_present=False
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-mcp running" in result.stdout
        assert "'ss' (iproute2) not found" in result.stdout
