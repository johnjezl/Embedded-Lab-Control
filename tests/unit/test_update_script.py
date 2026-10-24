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

    SNIPPET_MARKER = 'if ! "$LABCTL_VENV/bin/python" -I - "$cfg" "$key"'

    def test_set_and_missing(self, write_config):
        config = write_config("mcp:\n  allowed_read_paths: [/var/lib/labctl/images]\n")
        snippet = _snippet(self.SNIPPET_MARKER)
        assert _run(snippet, str(config), "allowed_read_paths").returncode == 0
        assert _run(snippet, str(config), "allowed_write_paths").returncode == 1

    def test_upload_dir(self, write_config):
        snippet = _snippet(self.SNIPPET_MARKER)
        config = write_config("mcp:\n  upload_dir: /var/lib/labctl/images\n")
        assert _run(snippet, str(config), "upload_dir").returncode == 0
        config = write_config("mcp: {}\n", name="off.yaml")
        assert _run(snippet, str(config), "upload_dir").returncode == 1


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
    listening socket.

    ``timeline`` maps a service to ``(from_sleep, ActiveState, NRestarts)``
    steps, counted in the script's ``sleep`` calls, as systemd reports a failed
    start it restarts: e.g. "deactivating", then "activating" (no MainPID,
    not listening) for RestartSec, then "active" with NRestarts 1.
    Without one, a running service is active with NRestarts 0. As in
    systemd, `systemctl restart` resets NRestarts (``restarts_before``
    until then)."""
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
        cgroup_path: str = "/system.slice/labctl-mcp.service",
        root_cgroup_pids=(),
        cgroup_v1: bool = False,
        extra_env=None,
        timeline=None,
        restarts_before: int = 0,
    ):
        sleeps, restarted = tmp_path / "sleeps", tmp_path / "restarted"
        steps = []
        for name, up in active.items():
            for start, state, count in (timeline or {}).get(
                name, [(0, "active" if up else "failed", 0)]
            ):
                steps.append(
                    f'  [ "$1" = {name} ] && [ "$n" -ge {start} ]'
                    f' && r="{state} {count}"'
                )
        systemctl = [
            "#!/bin/bash",
            "# info SVC: its ActiveState and NRestarts now",
            "info() {",
            f"  local n r='failed 0'; n=$(cat {sleeps} 2>/dev/null | wc -l)",
            *steps,
            '  echo "$r"',
            "}",
            'state() { set -- $(info "$1"); echo "$1"; }',
            "nrestarts() {",
            f'  if [ -e {restarted} ]; then set -- $(info "$1"); echo "$2";',
            f"  else echo {restarts_before}; fi",
            "}",
            'case "$1" in',
            "  is-enabled) exit 0 ;;",
            f"  restart) touch {restarted}; exit 0 ;;",
            '  show) [ "$4" = -p ] && {',
            '    echo "ActiveState=$(state "$6")"; echo "NRestarts=$(nrestarts "$6")"',
            "    exit 0; }",
            '    case "$3" in',
            '    MainPID) [ "$(state labctl-mcp)" = active ] && echo 4242 || echo 0 ;;',
            f"    ControlGroup) echo {cgroup_path} ;;",
            '    ActiveState) state "$5" ;;',
            '    NRestarts) nrestarts "$5" ;;',
            "  esac ;;",
            '  is-active) [ "$(state "$3")" = active ] && exit 0; exit 3 ;;',
            "esac",
        ]
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
        cgroup = (
            tmp_path / "cgroup" / ("systemd" if cgroup_v1 else "") / "system.slice"
        ) / "labctl-mcp.service"
        cgroup.mkdir(parents=True, exist_ok=True)
        (cgroup / "cgroup.procs").write_text("".join(f"{p}\n" for p in cgroup_pids))
        (tmp_path / "cgroup" / "cgroup.procs").write_text(
            "".join(f"{p}\n" for p in root_cgroup_pids)
        )
        idle = 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=42420,fd=3))'
        (stubs / "ss").write_text(
            f'#!/bin/bash\nif [ "$(systemctl show -p ActiveState --value labctl-mcp)"'
            f" = active ]; then echo '{ss_line}'; else echo '{idle}'; fi\n"
        )
        (stubs / "sleep").write_text(f"#!/bin/sh\necho x >> {sleeps}\n")
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "CGROUP_ROOT": str(tmp_path / "cgroup"),
            "SS_CMD": "ss" if ss_present else "no-such-ss-command",
            **(extra_env or {}),
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

    def test_waits_for_systemd_auto_restart(self, run_restart):
        """A failed start (e.g. the old server's port still briefly in use)
        is "deactivating", then "activating" until systemd restarts it
        after RestartSec: update.sh waits, and says a start failed."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            timeline={
                "labctl-mcp": [
                    (0, "deactivating", 0),  # longer than the others' checks
                    (6, "activating", 0),
                    (16, "activating", 1),  # restart queued: already counted
                    (17, "active", 1),
                ]
            },
            restarts_before=3,
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-mcp running" in result.stdout
        assert "labctl-mcp is up only after a failed start" in result.stdout
        assert "restarted it\n     1 time(s)" in result.stdout

    def test_restarts_before_this_update_not_reported(self, run_restart):
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": "MCP HTTP: API keys required"}, restarts_before=5
        )
        assert result.returncode == 0, result.stdout
        assert "failed start" not in result.stdout

    def test_other_service_failing_after_a_moment_waited_for(self, run_restart):
        """Type=simple: active as soon as it starts, then the start fails
        (e.g. its port still in use) and systemd restarts it."""
        result, _ = run_restart(
            ALL_UP,
            {
                "labctl-mcp": "MCP HTTP: API keys required",
                "labctl-web": "OSError: [Errno 98] Address already in use",
            },
            timeline={
                "labctl-web": [
                    (0, "active", 0),  # active on the first 4 checks
                    (5, "deactivating", 0),
                    (6, "activating", 0),
                    (16, "active", 1),
                ]
            },
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-web running" in result.stdout
        assert "labctl-web is up only after a failed start" in result.stdout
        assert "Address already in use" in result.stdout

    def test_auto_restart_without_ss(self, run_restart):
        """No `ss`: still waits for the unit to be active again."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            ss_present=False,
            timeline={"labctl-mcp": [(0, "activating", 0), (12, "active", 1)]},
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-mcp running" in result.stdout
        assert "Can't confirm labctl-mcp is listening" in result.stdout

    def test_auto_restart_that_never_binds_is_failed(self, run_restart):
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            timeline={"labctl-mcp": [(0, "activating", 0)]},
            extra_env={"MCP_START_TIMEOUT": "8"},
        )
        assert result.returncode == 1
        assert "[!!] labctl-mcp FAILED: still restarting after 8s" in result.stdout

    def test_restart_loop_stops_waiting(self, run_restart, tmp_path):
        """Failing again after systemd's restarts isn't transient: report
        it once systemd has restarted it twice, without waiting out the
        timeout."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "ModuleNotFoundError: No module named 'mcp'"},
            listening=False,
            timeline={
                "labctl-mcp": [
                    (0, "activating", 0),
                    (10, "active", 1),
                    (11, "activating", 1),
                    (21, "active", 2),
                    (22, "activating", 2),
                    (32, "active", 3),
                ]
            },
        )
        assert result.returncode == 1
        assert (
            "[!!] labctl-mcp FAILED: systemd has restarted it 2 times" in result.stdout
        )
        assert len((tmp_path / "sleeps").read_text().splitlines()) < 30

    def test_service_not_yet_stable_is_flagged(self, run_restart):
        """The wait ended before labctl-web had been active long enough to
        trust: up, with a warning."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            extra_env={"MCP_START_TIMEOUT": "4"},  # one round of checks
        )
        assert "[ok] labctl-web running" in result.stdout
        assert "labctl-web only just (re)started and may still fail" in result.stdout

    def test_settled_services_not_flagged(self, run_restart):
        result, _ = run_restart(ALL_UP, {"labctl-mcp": "MCP HTTP: API keys required"})
        assert result.returncode == 0, result.stdout
        assert "only just" not in result.stdout

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

    def test_no_cgroup_does_not_match_root_cgroup(self, run_restart):
        """Unit stopped/torn down (empty ControlGroup): the root cgroup's
        processes (here one listening) must not count as labctl-mcp's."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            listener_pid=777,
            cgroup_path="",
            root_cgroup_pids=(777,),
        )
        assert "[!!] labctl-mcp FAILED" in result.stdout

    def test_cgroup_v1_wrapper_child_counts(self, run_restart):
        """v1/hybrid hosts keep the unit's PIDs under the "systemd" tree."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            listener_pid=4243,
            cgroup_pids=(4242, 4243),
            cgroup_v1=True,
        )
        assert result.returncode == 0, result.stdout
        assert "[ok] labctl-mcp running" in result.stdout

    def test_not_listening_message_names_timeout(self, run_restart):
        result, _ = run_restart(
            ALL_UP, {"labctl-mcp": "MCP HTTP: API keys required"}, listening=False
        )
        assert "running but not listening after 60s" in result.stdout
        assert "MCP_START_TIMEOUT=120" in result.stdout

    @pytest.mark.parametrize("value,shown", [("2m", "60s"), ("2", "4s")])
    def test_start_timeout_validated(self, run_restart, value, shown):
        """A non-integer falls back to 60 s; too small is raised to the
        minimum the wait actually takes, so the message is true."""
        result, _ = run_restart(
            ALL_UP,
            {"labctl-mcp": "MCP HTTP: API keys required"},
            listening=False,
            extra_env={"MCP_START_TIMEOUT": value},
        )
        assert f"not listening after {shown}" in result.stdout


class TestRootPythonIsolation:
    """Root-run Python in the install/update scripts must not import from
    the admin's current directory (a group-writable images/ could hold a
    planted `labctl/` package)."""

    SCRIPTS = [UPDATE_SH, UPDATE_SH.parent / "install-services.sh"]

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_runs_from_root_dir_before_any_python(self, script):
        lines = script.read_text().splitlines()
        cd_root = lines.index("cd /")
        first_python = next(
            i
            for i, line in enumerate(lines)
            if "bin/python" in line and "#" not in line
        )
        assert cd_root < first_python

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_root_python_is_isolated(self, script):
        calls = [
            line
            for line in script.read_text().splitlines()
            if '"$LABCTL_VENV/bin/python"' in line and not line.lstrip().startswith("#")
        ]
        assert calls
        assert all('/bin/python" -I ' in line for line in calls), calls


class TestPreInstallConfigCheck:
    """Before installing, update.sh loads each config with the new
    release's config.py (alone: no labctl.core package imports)."""

    @staticmethod
    def _script() -> str:
        text = UPDATE_SH.read_text()
        start = text.index('YAML_ERR=$("$LABCTL_VENV/bin/python" -I -c \'\n') + len(
            'YAML_ERR=$("$LABCTL_VENV/bin/python" -I -c \'\n'
        )
        return text[start : text.index('\n\' "$cfg"', start)]

    def run(self, cfg, loader=None):
        loader = loader or (UPDATE_SH.parents[1] / "src/labctl/core/config.py")
        return subprocess.run(
            [sys.executable, "-I", "-c", self._script(), str(cfg), str(loader)],
            capture_output=True,
            text=True,
            cwd="/",
            timeout=60,
        )

    @pytest.mark.parametrize(
        "body,rc",
        [
            ("log_level: INFO\nmcp:\n#  commented out\n", 0),
            ("auth: [unclosed\n", 1),
            ("database:\n  timeout_seconds: 10s\n", 1),
        ],
    )
    def test_results(self, tmp_path, body, rc):
        cfg = tmp_path / "c.yaml"
        cfg.write_text(body)
        assert self.run(cfg).returncode == rc

    def test_check_failure_is_not_a_config_error(self, tmp_path):
        cfg = tmp_path / "c.yaml"
        cfg.write_text("log_level: INFO\n")
        assert self.run(cfg, loader=tmp_path / "missing.py").returncode == 3
