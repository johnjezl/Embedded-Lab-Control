"""
Status checks for systemd services labctl depends on.

Provides a single call that reports on all of labctl's own services
plus external dependencies (ser2net). Used by `labctl services status`
to give operators a quick at-a-glance health view without having to
run `systemctl` against each unit individually.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# Units labctl depends on. First three are labctl's own; ser2net is
# the hardware-facing TCP-to-serial proxy used for every console.
DEFAULT_UNITS: tuple[str, ...] = (
    "labctl-monitor",
    "labctl-mcp",
    "labctl-web",
    "ser2net",
)


@dataclass
class ServiceStatus:
    """One service's health snapshot."""

    unit: str
    active_state: str = "unknown"  # active | inactive | failed | activating | ...
    sub_state: str = ""  # running | dead | exited | ...
    active_since: Optional[datetime] = None
    n_restarts: int = 0
    exec_main_status: Optional[int] = None
    result: str = ""  # success | exit-code | signal | oom-kill | ...
    recent_errors: list[str] = field(default_factory=list)
    error: Optional[str] = None  # set if systemctl itself failed

    @property
    def healthy(self) -> bool:
        return self.active_state == "active" and self.sub_state in {"running", "exited"}

    def uptime_str(self) -> str:
        if not self.active_since:
            return "-"
        delta = datetime.now().astimezone() - self.active_since
        return _format_duration(delta)


def _format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        return "-"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    if mins:
        return f"{mins}m{secs}s"
    return f"{secs}s"


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a subprocess and capture combined output."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def _parse_systemctl_show(output: str) -> dict[str, str]:
    """Parse `systemctl show -p Key1,Key2 unit` KEY=VALUE lines."""
    props: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def _parse_systemd_timestamp(value: str) -> Optional[datetime]:
    """Parse a systemd timestamp like 'Sun 2026-04-19 16:08:26 PDT'.

    Returns None for empty / sentinel values ("n/a", "0", "").
    """
    if not value or value in {"n/a", "0"}:
        return None
    # Format example: "Sun 2026-04-19 16:08:26 PDT"
    for fmt in (
        "%a %Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S %Z",
        "%a %Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.astimezone()
        except ValueError:
            continue
    return None


def _collect_recent_errors(unit: str, hours: int = 24, limit: int = 3) -> list[str]:
    """Pull the last few distinct error/warning messages from the unit's journal.

    Uses journalctl with priority <= err. Returns at most `limit` lines.
    Silently returns [] if journalctl isn't available.
    """
    if shutil.which("journalctl") is None:
        return []
    code, out = _run(
        [
            "journalctl",
            "-u",
            unit,
            f"--since={hours} hours ago",
            "--no-pager",
            "-q",
            "-p",
            "err",
            "-o",
            "short-iso",
        ],
        timeout=5.0,
    )
    if code != 0:
        return []
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # De-duplicate while preserving order, keep the last `limit` distinct.
    seen: set[str] = set()
    uniq: list[str] = []
    for ln in reversed(lines):
        # Strip leading timestamp + hostname + unit[pid]: to dedupe by message.
        msg = ln.split(": ", 1)[-1] if ": " in ln else ln
        if msg in seen:
            continue
        seen.add(msg)
        uniq.append(ln)
        if len(uniq) >= limit:
            break
    return list(reversed(uniq))


def check_service(unit: str) -> ServiceStatus:
    """Check one systemd unit and return a structured status."""
    status = ServiceStatus(unit=unit)

    if shutil.which("systemctl") is None:
        status.error = "systemctl not found"
        return status

    code, out = _run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,"
            "ExecMainStatus,Result,LoadState",
        ]
    )
    if code != 0:
        status.error = out.strip() or f"systemctl show exited {code}"
        return status

    props = _parse_systemctl_show(out)
    if props.get("LoadState") in {"not-found", "masked"}:
        status.error = f"unit not loaded (LoadState={props.get('LoadState')})"
        status.active_state = "not-found"
        return status

    status.active_state = props.get("ActiveState", "unknown")
    status.sub_state = props.get("SubState", "")
    status.active_since = _parse_systemd_timestamp(
        props.get("ActiveEnterTimestamp", "")
    )
    try:
        status.n_restarts = int(props.get("NRestarts", "0"))
    except ValueError:
        status.n_restarts = 0
    try:
        status.exec_main_status = int(props.get("ExecMainStatus", "0"))
    except ValueError:
        status.exec_main_status = None
    status.result = props.get("Result", "")

    if not status.healthy or status.n_restarts > 0:
        status.recent_errors = _collect_recent_errors(unit)

    return status


def check_all(units: tuple[str, ...] = DEFAULT_UNITS) -> list[ServiceStatus]:
    """Check all services. Returned in the order given."""
    return [check_service(u) for u in units]
