#!/bin/bash
# Update labctl installation (preserves config and database)
#
# Usage: sudo ./scripts/update.sh
#
# This script:
#   1. Reinstalls labctl into the production venv with all extras
#   2. Restarts all labctl services
#   3. Verifies services started successfully

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
# Run from "/": root runs Python below, and `python -m`/`python -` look in
# the current directory first, so a `labctl/` package planted where the
# admin happens to be (e.g. the group-writable images/ directory) would run
# as root. Root Python calls also use -I (isolated: no cwd, no PYTHON* env).
cd /
LABCTL_VENV="/opt/labctl/venv"
SYSTEMD_CONFIG_DIR="$PROJECT_DIR/config/systemd"
SYSTEM_CONFIG_DIR="/etc/labctl"
SYSTEM_CONFIG_FILE="$SYSTEM_CONFIG_DIR/config.yaml"
# The labctl home, with any admin symlink (e.g. -> /data/labctl) resolved
# once: its own directory entry lives in root-owned /var/lib, so labctl
# can't redirect it. Paths *inside* it are labctl-writable and are only
# touched through labctl.core.admin_files.
LABCTL_HOME=$(realpath -e /var/lib/labctl 2>/dev/null || echo /var/lib/labctl)
if [ ! -d "$LABCTL_HOME" ]; then
    echo "Error: labctl home $LABCTL_HOME is missing."
    echo "Run scripts/install-services.sh to recreate it."
    exit 1
fi
SERVICE_CONFIG_FILE="$LABCTL_HOME/.config/labctl/config.yaml"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Error: Please run as root (sudo)"
    exit 1
fi

echo "=== labctl Update ==="
echo ""

# Check venv exists
if [ ! -x "$LABCTL_VENV/bin/pip" ]; then
    echo "Error: Production venv not found at $LABCTL_VENV"
    echo "Run scripts/install-services.sh for first-time setup."
    exit 1
fi

# Refuse early, before installing anything, if the service copy of the
# config is a symlink, not a regular file, or hard-linked: step 3 refuses
# it anyway (race-free, via admin_files), and stopping there would leave
# a new venv and units with the old services still running.
# Every component below the labctl home counts, as in admin_files.
for rel in .config .config/labctl .config/labctl/config.yaml; do
    if [ -L "$LABCTL_HOME/$rel" ]; then
        echo "Error: $LABCTL_HOME/$rel is a symlink; the service config path"
        echo "must not contain one. Replace it with a real directory/file, then rerun."
        exit 1
    fi
done
for rel in .config .config/labctl; do
    if [ -e "$LABCTL_HOME/$rel" ] && [ ! -d "$LABCTL_HOME/$rel" ]; then
        echo "Error: $LABCTL_HOME/$rel is not a directory; fix it, then rerun."
        exit 1
    fi
done
if [ -e "$SERVICE_CONFIG_FILE" ] && { [ ! -f "$SERVICE_CONFIG_FILE" ] ||
    [ "$(stat -c %h "$SERVICE_CONFIG_FILE")" != 1 ]; }; then
    echo "Error: $SERVICE_CONFIG_FILE is not a regular file, or is hard-linked;"
    echo "replace it with a regular file, then rerun."
    exit 1
fi
# Same for config drift (step 3 checks again; the by-path read is #19).
if [ -f "$SYSTEM_CONFIG_FILE" ] && [ -f "$SERVICE_CONFIG_FILE" ] &&
    ! cmp -s "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; then
    echo "Error: $SYSTEM_CONFIG_FILE differs from $SERVICE_CONFIG_FILE."
    echo "Sync the files intentionally, then rerun scripts/update.sh."
    exit 1
fi
# And a config that doesn't load (YAML error, or a value labctl rejects,
# e.g. `timeout_seconds: 10s`): the services would fall back to defaults
# (labctl-mcp over HTTP refuses to start). Checked with the to-be-installed
# release's loader (imported from this checkout; the installed venv's
# PyYAML), before anything changes.
for cfg in "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; do
    [ -f "$cfg" ] || continue
    # Exit 3: the check itself couldn't run (warn, don't block updates).
    YAML_RC=0
    YAML_ERR=$("$LABCTL_VENV/bin/python" -I -c '
import importlib.util, logging, sys
from pathlib import Path
logging.disable(logging.WARNING)
try:
    # config.py alone (stdlib + PyYAML), not the labctl.core package.
    spec = importlib.util.spec_from_file_location("labctl_config_check", sys.argv[2])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up
    spec.loader.exec_module(mod)
    errors = mod.load_config(Path(sys.argv[1])).load_errors
except Exception as e:
    print(f"{type(e).__name__}: {e}")
    sys.exit(3)
if errors:
    print("\n".join(errors))
    sys.exit(1)
' "$cfg" "$PROJECT_DIR/src/labctl/core/config.py" 2>&1) || YAML_RC=$?
    if [ "$YAML_RC" -eq 3 ]; then
        echo "[!!] Could not check $cfg with the new release's loader:"
        tail -n 2 <<<"$YAML_ERR" | sed 's/^/     /'
    elif [ "$YAML_RC" -ne 0 ]; then
        echo "Error: $cfg doesn't load; fix it, then rerun:"
        tail -n 4 <<<"$YAML_ERR" | sed 's/^/     /'
        exit 1
    fi
done

# 1. Reinstall package
# 0.2.0 renamed the distribution labctl -> embedded-lab-control. Both own the
# same labctl/ package and `labctl` script, so remove the old one first:
# installed side by side, a later `pip uninstall labctl` would delete the new
# install's files. One-time migration; a no-op once the old dist is gone.
# (If the install below then fails, running services keep their loaded code;
# fix the error and rerun this script.)
if "$LABCTL_VENV/bin/pip" show labctl &>/dev/null; then
    echo "[+] Removing old 'labctl' distribution (renamed embedded-lab-control)..."
    "$LABCTL_VENV/bin/pip" uninstall -y labctl --quiet
fi

echo "[+] Installing labctl from $PROJECT_DIR..."
"$LABCTL_VENV/bin/pip" install "$PROJECT_DIR[web,mcp,kasa,sdwire]" --quiet
echo "[ok] Package installed"

# 2. Refresh installed service definitions
echo "[+] Refreshing systemd unit files..."
cp "$SYSTEMD_CONFIG_DIR/labctl-monitor.service" /etc/systemd/system/
cp "$SYSTEMD_CONFIG_DIR/labctl-web.service" /etc/systemd/system/
cp "$SYSTEMD_CONFIG_DIR/labctl-mcp.service" /etc/systemd/system/
systemctl daemon-reload
echo "[ok] systemd unit files refreshed"

# 3. Repair shared config permissions and seed if missing
echo "[+] Checking shared config..."
mkdir -p "$SYSTEM_CONFIG_DIR"
SYSTEM_CONFIG_DIR=$(realpath -e "$SYSTEM_CONFIG_DIR")  # an admin symlink is fine
SYSTEM_CONFIG_FILE="$SYSTEM_CONFIG_DIR/config.yaml"
# The service copy lives under /var/lib/labctl, which the labctl user can
# write: root only touches it through labctl.core.admin_files (no symlinks,
# no hard links, descriptor-based chown/chmod, O_EXCL copies).
ADMIN_FILES=("$LABCTL_VENV/bin/python" -I -m labctl.core.admin_files)
if ! SERVICE_CONFIG_STATE=$("${ADMIN_FILES[@]}" state "$SERVICE_CONFIG_FILE"); then
    echo "[!!] $SERVICE_CONFIG_STATE"
    echo "     Not touching $SERVICE_CONFIG_FILE; replace it with a regular file."
    exit 1
fi
if [ ! -f "$SYSTEM_CONFIG_FILE" ]; then
    if [ "$SERVICE_CONFIG_STATE" = regular ]; then
        "${ADMIN_FILES[@]}" copy "$SERVICE_CONFIG_FILE" "$SYSTEM_CONFIG_FILE" 640
        echo "[ok] Installed missing $SYSTEM_CONFIG_FILE from service config"
    else
        echo "[!!] Missing both $SYSTEM_CONFIG_FILE and $SERVICE_CONFIG_FILE"
        echo "Run scripts/install-services.sh for first-time setup."
        exit 1
    fi
elif [ "$SERVICE_CONFIG_STATE" = regular ] && ! cmp -s "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; then
    echo "[!!] Shared config drift detected:"
    echo "     $SYSTEM_CONFIG_FILE differs from $SERVICE_CONFIG_FILE"
    echo "Refusing to restart services onto a stale /etc config."
    echo "Sync the files intentionally, then rerun scripts/update.sh."
    exit 1
fi
chown root:labctl "$SYSTEM_CONFIG_DIR" "$SYSTEM_CONFIG_FILE"
chmod 750 "$SYSTEM_CONFIG_DIR"
chmod 640 "$SYSTEM_CONFIG_FILE"
# The service copy carries the same secrets (API keys, Kasa credentials)
# and sits under /var/lib/labctl, which may be traversable by others.
if [ "$SERVICE_CONFIG_STATE" = regular ]; then
    "${ADMIN_FILES[@]}" secure "$SERVICE_CONFIG_FILE" labctl labctl 640
fi
echo "[ok] Shared config permissions repaired"

# 3b. MCP host-file allowlist (0.2.0+): deny-all unless configured. Create
# the default directories if missing (never touching existing ones), and
# point at the config keys if they aren't set. Config files are not edited.
# output/ is also sticky (3775), as in install-services.sh.
for spec in "$LABCTL_HOME/images:2775" "$LABCTL_HOME/output:3775"; do
    dir="${spec%%:*}"
    mode="${spec##*:}"
    if [ ! -e "$dir" ] && [ ! -L "$dir" ]; then
        "${ADMIN_FILES[@]}" secure-dir "$dir" labctl labctl "$mode"
        echo "[ok] Created $dir (mode $mode)"
    fi
done
# Check both keys in both files (the systemd unit passes -c /etc/...;
# a `labctl mcp` run as labctl without -c reads the service config first;
# the drift check above keeps them identical). Parse with labctl's own
# loader (installed in step 1), so YAML flow style, empty lists and keys
# under the wrong section are judged exactly as the server will.
MISSING_ALLOWLIST=""
MISSING_UPLOAD=""
for cfg in "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; do
    [ -f "$cfg" ] || continue
    for key in allowed_read_paths allowed_write_paths upload_dir; do
        if ! "$LABCTL_VENV/bin/python" -I - "$cfg" "$key" 2>/dev/null <<'PY'
import sys
from pathlib import Path

from labctl.core.config import load_config

config = load_config(Path(sys.argv[1]))
sys.exit(0 if getattr(config.mcp, sys.argv[2]) else 1)
PY
        then
            if [ "$key" = upload_dir ]; then
                MISSING_UPLOAD="$MISSING_UPLOAD $cfg"
            else
                MISSING_ALLOWLIST="$MISSING_ALLOWLIST $cfg:$key"
            fi
        fi
    done
done
if [ -n "$MISSING_ALLOWLIST" ]; then
    echo "[!!] MCP host file access is deny-all where not configured; missing:"
    for item in $MISSING_ALLOWLIST; do echo "       $item"; done
    echo "     flash_image / sdwire_update (read) and boot_test output_dir"
    echo "     (write) over MCP refuse host paths until set. To allow the"
    echo "     default directories, add to both config files:"
    echo "       mcp:"
    echo "         allowed_read_paths: [/var/lib/labctl/images]"
    echo "         allowed_write_paths: [/var/lib/labctl/output]"
fi
if [ -n "$MISSING_UPLOAD" ]; then
    echo "[--] MCP image uploads (PUT /images/<name>) are off in:"
    for item in $MISSING_UPLOAD; do echo "       $item"; done
    echo "     To let MCP clients upload builds for flash_image /"
    echo "     sdwire_update / boot_test, add to both config files (inside"
    echo "     allowed_read_paths):"
    echo "       mcp:"
    echo "         upload_dir: /var/lib/labctl/images"
fi

# 3c. The services' database: when database_path is in $LABCTL_HOME/db
# (where install-services.sh puts a new install's), keep that directory setgid
# 2775 and the database (and any -wal/-shm) 664, so labctl group members
# can write it from the CLI. A database elsewhere is left alone.
LABCTL_DB_DIR="$LABCTL_HOME/db"
# Root reads only root's own /etc/labctl/config.yaml here.
DB_PATH=$("$LABCTL_VENV/bin/python" -I -c '
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)
print(data.get("database_path", "") if isinstance(data, dict) else "")
' "$SYSTEM_CONFIG_FILE")
if [ "$DB_PATH" = "$LABCTL_DB_DIR/labctl.db" ]; then
    if [ -L "$LABCTL_DB_DIR" ]; then
        echo "[ok] $LABCTL_DB_DIR is a symlink; leaving it (and its permissions) as is"
    else
        "${ADMIN_FILES[@]}" secure-dir "$LABCTL_DB_DIR" labctl labctl 2775
        "${ADMIN_FILES[@]}" create "$DB_PATH" labctl labctl 664
        for extra in "$DB_PATH-wal" "$DB_PATH-shm"; do
            if [ "$("${ADMIN_FILES[@]}" state "$extra")" = regular ]; then
                "${ADMIN_FILES[@]}" secure "$extra" labctl labctl 664
            fi
        done
        echo "[ok] Database $DB_PATH (labctl:labctl, 664; group-writable directory)"
    fi
else
    echo "[--] The services' database (${DB_PATH:-database_path unset}) isn't in"
    echo "     $LABCTL_DB_DIR, so labctl group members can't write it from the CLI."
    echo "     To move it there, see \"Moving the database\" in README.md."
fi

# 4. Verify install
VERSION=$("$LABCTL_VENV/bin/labctl" --version 2>&1 || true)
echo "[ok] $VERSION"

# 5. Restart services
SERVICES=""
for svc in labctl-web labctl-monitor labctl-mcp; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        SERVICES="$SERVICES $svc"
    fi
done

if [ -n "$SERVICES" ]; then
    echo "[+] Restarting services:$SERVICES"
    # Report journal lines from this restart only: everything after a
    # cursor taken now (whole-second --since is the fallback).
    JOURNAL_CURSOR=$(journalctl -n 0 --show-cursor --no-pager 2>/dev/null | sed -n 's/^-- cursor: //p' || true)
    RESTART_SINCE=$(date '+%Y-%m-%d %H:%M:%S')
    svc_log() {
        if [ -n "$JOURNAL_CURSOR" ]; then
            journalctl -u "$1" --after-cursor "$JOURNAL_CURSOR" --no-pager -o cat 2>/dev/null || true
        else
            journalctl -u "$1" --since "$RESTART_SINCE" --no-pager -o cat 2>/dev/null || true
        fi
    }
    # unit_info SVC: sets UNIT_STATE (ActiveState) and UNIT_RESTARTS (how
    # many times systemd has restarted it after a failed start, under
    # Restart=on-failure, since `systemctl restart`, which resets the count).
    unit_info() {
        local key value
        UNIT_STATE=""
        UNIT_RESTARTS=0
        while IFS='=' read -r key value; do
            case "$key" in
                ActiveState) UNIT_STATE=$value ;;
                NRestarts) case "$value" in '' | *[!0-9]*) ;; *) UNIT_RESTARTS=$value ;; esac ;;
            esac
        done < <(systemctl show -p ActiveState -p NRestarts "$1" 2>/dev/null || true)
    }
    systemctl restart $SERVICES
    sleep 3
    # labctl-mcp is only up once one of its processes has a listening TCP
    # socket (startup checks, SDK import, then the bind; a port in use makes
    # it exit). Any process of the unit counts, not just MainPID: a wrapper
    # ExecStart (sh -c ...) leaves the server as a child. Returns 0 when
    # listening, 1 when not, 2 when it can't tell (no `ss`).
    mcp_listening() {
        local cg pids pattern
        command -v "${SS_CMD:-ss}" >/dev/null 2>&1 || return 2
        cg=$(systemctl show -p ControlGroup --value labctl-mcp 2>/dev/null || true)
        # No cgroup (unit stopped): don't fall back to the root cgroup's PIDs.
        # Unified (v2) hierarchy first, then the v1 "systemd" one.
        pids=$( { if [ -n "$cg" ]; then
                      cat "${CGROUP_ROOT:-/sys/fs/cgroup}$cg/cgroup.procs" 2>/dev/null ||
                          cat "${CGROUP_ROOT:-/sys/fs/cgroup}/systemd$cg/cgroup.procs" 2>/dev/null
                  fi || true
                  systemctl show -p MainPID --value labctl-mcp 2>/dev/null || true; } |
                grep -E '^[1-9][0-9]*$' | sort -u | paste -sd'|' -)
        [ -n "$pids" ] || return 1
        pattern="pid=($pids),"
        "${SS_CMD:-ss}" -ltnpH 2>/dev/null | grep -qE "$pattern"
    }
    # Slow hosts (cold cache after the reinstall, SBC-class CPUs) can take a
    # while to import the SDK and bind: MCP_START_TIMEOUT (seconds) to wait
    # for the services to settle (through a systemd restart after a failed
    # start, RestartSec=10) and for labctl-mcp to listen.
    MCP_START_TIMEOUT=${MCP_START_TIMEOUT:-60}
    case "$MCP_START_TIMEOUT" in
        ''|*[!0-9]*)
            echo "[!!] MCP_START_TIMEOUT must be whole seconds; using 60"
            MCP_START_TIMEOUT=60 ;;
    esac
    if [ "$MCP_START_TIMEOUT" -lt 4 ]; then MCP_START_TIMEOUT=4; fi
    # A unit whose start failed is "deactivating" while its last processes
    # exit, then "activating" while systemd waits RestartSec to restart it
    # (Restart=on-failure). Wait while any unit is in between, and until
    # labctl-mcp listens, up to the timeout. settling SVC: 0 while waiting.
    settling() {
        unit_info "$1"
        case "$UNIT_STATE" in
            active)
                [ "$1" = labctl-mcp ] || return 1
                MCP_LISTEN=0
                mcp_listening || MCP_LISTEN=$?
                [ "$MCP_LISTEN" -eq 1 ] ;; # listening, or can't tell: done
            activating | deactivating | reloading) return 0 ;;
            *) return 1 ;;
        esac
    }
    # Seconds, however long each round of checks takes (the bound on rounds
    # keeps a stubbed `sleep` finite).
    WAIT_UNTIL=$((SECONDS + MCP_START_TIMEOUT - 3))
    for _ in $(seq 4 "$MCP_START_TIMEOUT"); do
        PENDING=""
        for svc in $SERVICES; do
            if settling "$svc"; then PENDING="$PENDING $svc"; fi
        done
        [ -n "$PENDING" ] || break
        [ "$SECONDS" -lt "$WAIT_UNTIL" ] || break
        sleep 1
    done
    if [[ " $SERVICES " == *" labctl-mcp "* ]]; then
        # journald stores stdout asynchronously: give the auth-mode line,
        # printed before the bind, a moment to arrive (if it's still up).
        for _ in 1 2 3 4 5; do
            if ! systemctl is-active --quiet labctl-mcp; then break; fi
            if svc_log labctl-mcp | grep -q '^MCP HTTP: '; then break; fi
            sleep 1
        done
    fi

    # 6. Verify services
    FAILED=""
    for svc in $SERVICES; do
        # One reading of the unit for the whole verdict.
        unit_info "$svc"
        SVC_ACTIVE=no
        case "$UNIT_STATE" in active | reloading) SVC_ACTIVE=yes ;; esac
        # Running but not listening (e.g. still exiting after a failed bind).
        if [ "$svc" = labctl-mcp ] && [ "$SVC_ACTIVE" = yes ]; then
            MCP_LISTEN=0
            mcp_listening || MCP_LISTEN=$?
            if [ "$MCP_LISTEN" -eq 1 ]; then
                SVC_ACTIVE=no
            elif [ "$MCP_LISTEN" -eq 2 ]; then
                echo "[!!] Can't confirm labctl-mcp is listening: 'ss' (iproute2) not found"
            fi
        fi
        if [ "$SVC_ACTIVE" = yes ]; then
            echo "[ok] $svc running"
            if [ "$UNIT_RESTARTS" -gt 0 ]; then
                echo "[!!] $svc is up after a failed start: systemd restarted it"
                echo "     $UNIT_RESTARTS time(s) (log below)"
                if [ "$svc" != labctl-mcp ]; then
                    svc_log "$svc" | tail -n 15 | sed 's/^/     /'
                fi
            fi
            if [ "$svc" = labctl-mcp ]; then
                # Auth mode (0.2.0+: API keys when auth.enabled) and startup
                # warnings. Search the whole log, show only its tail.
                MCP_LOG=$(svc_log labctl-mcp)
                if [ -n "$MCP_LOG" ]; then tail -n 15 <<<"$MCP_LOG" | sed 's/^/     /'; fi
                if grep -q '^MCP HTTP: API keys required' <<<"$MCP_LOG"; then
                    echo "[!!] auth.enabled is on: MCP HTTP clients must send"
                    echo "     'Authorization: Bearer <api_key>' (a user's api_key from auth.users)."
                elif ! grep -q '^MCP HTTP: ' <<<"$MCP_LOG"; then
                    echo "[!!] labctl-mcp hasn't logged its auth mode yet; check it with:"
                    echo "     journalctl -u labctl-mcp -n 30"
                fi
            fi
        else
            if [ "$svc" = labctl-mcp ] && [ "$UNIT_STATE" = active ]; then
                echo "[!!] $svc FAILED: running but not listening after ${MCP_START_TIMEOUT}s"
                echo "     (still starting on a slow host? rerun with MCP_START_TIMEOUT=120);"
                echo "     log since restart:"
            elif [ "$UNIT_STATE" = activating ]; then
                echo "[!!] $svc FAILED: still restarting after ${MCP_START_TIMEOUT}s (systemd"
                echo "     restarts a failed start; slow host? rerun with MCP_START_TIMEOUT=120);"
                echo "     log since restart:"
            elif [ "$UNIT_STATE" = deactivating ]; then
                echo "[!!] $svc FAILED: still stopping after ${MCP_START_TIMEOUT}s; check it with"
                echo "     systemctl status $svc; log since restart:"
            else
                echo "[!!] $svc FAILED; log since restart:"
            fi
            svc_log "$svc" | tail -n 15 | sed 's/^/     /'
            FAILED="$FAILED $svc"
        fi
    done

    if [ -n "$FAILED" ]; then
        echo ""
        echo "WARNING: Some services failed to start:$FAILED"
        echo "Check logs with: journalctl -u <service> --since '1 min ago'"
        exit 1
    fi
else
    echo "[ok] No enabled services to restart"
fi

echo ""
echo "=== Update Complete ==="
