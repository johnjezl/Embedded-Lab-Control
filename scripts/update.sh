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
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABCTL_VENV="/opt/labctl/venv"
SYSTEMD_CONFIG_DIR="$PROJECT_DIR/config/systemd"
SYSTEM_CONFIG_DIR="/etc/labctl"
SYSTEM_CONFIG_FILE="$SYSTEM_CONFIG_DIR/config.yaml"
SERVICE_CONFIG_FILE="/var/lib/labctl/.config/labctl/config.yaml"

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
if [ ! -f "$SYSTEM_CONFIG_FILE" ]; then
    if [ -f "$SERVICE_CONFIG_FILE" ]; then
        cp "$SERVICE_CONFIG_FILE" "$SYSTEM_CONFIG_FILE"
        echo "[ok] Installed missing $SYSTEM_CONFIG_FILE from service config"
    else
        echo "[!!] Missing both $SYSTEM_CONFIG_FILE and $SERVICE_CONFIG_FILE"
        echo "Run scripts/install-services.sh for first-time setup."
        exit 1
    fi
elif [ -f "$SERVICE_CONFIG_FILE" ] && ! cmp -s "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; then
    echo "[!!] Shared config drift detected:"
    echo "     $SYSTEM_CONFIG_FILE differs from $SERVICE_CONFIG_FILE"
    echo "Refusing to restart services onto a stale /etc config."
    echo "Sync the files intentionally, then rerun scripts/update.sh."
    exit 1
fi
chown root:labctl "$SYSTEM_CONFIG_DIR" "$SYSTEM_CONFIG_FILE"
chmod 750 "$SYSTEM_CONFIG_DIR"
chmod 640 "$SYSTEM_CONFIG_FILE"
echo "[ok] Shared config permissions repaired"

# 3b. MCP host-file allowlist (0.2.0+): deny-all unless configured. Create
# the default directories if missing (never touching existing ones), and
# point at the config keys if they aren't set. Config files are not edited.
for dir in /var/lib/labctl/images /var/lib/labctl/output; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        chown labctl:labctl "$dir"
        chmod 2775 "$dir"
        echo "[ok] Created $dir"
    fi
done
# Check both keys in both files (the systemd unit passes -c /etc/...;
# a `labctl mcp` run as labctl without -c reads the service config first;
# the drift check above keeps them identical). Parse with labctl's own
# loader (installed in step 1), so YAML flow style, empty lists and keys
# under the wrong section are judged exactly as the server will.
MISSING_ALLOWLIST=""
for cfg in "$SYSTEM_CONFIG_FILE" "$SERVICE_CONFIG_FILE"; do
    [ -f "$cfg" ] || continue
    for key in allowed_read_paths allowed_write_paths; do
        if ! "$LABCTL_VENV/bin/python" - "$cfg" "$key" 2>/dev/null <<'PY'
import sys
from pathlib import Path

from labctl.core.config import load_config

config = load_config(Path(sys.argv[1]))
sys.exit(0 if getattr(config.mcp, sys.argv[2]) else 1)
PY
        then
            MISSING_ALLOWLIST="$MISSING_ALLOWLIST $cfg:$key"
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
    systemctl restart $SERVICES
    sleep 2

    # 6. Verify services
    FAILED=""
    for svc in $SERVICES; do
        if systemctl is-active --quiet "$svc"; then
            echo "[ok] $svc running"
        else
            echo "[!!] $svc FAILED"
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
