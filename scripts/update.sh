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
echo "[+] Installing labctl from $PROJECT_DIR..."
"$LABCTL_VENV/bin/pip" install "$PROJECT_DIR[web,mcp,kasa,sdwire]" --quiet
echo "[ok] Package installed"

# 2. Verify install
VERSION=$("$LABCTL_VENV/bin/labctl" --version 2>&1 || true)
echo "[ok] $VERSION"

# 3. Restart services
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

    # 4. Verify services
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
