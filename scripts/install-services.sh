#!/bin/bash
# Install labctl system-wide with systemd services
#
# Usage: sudo ./scripts/install-services.sh
#
# This script:
#   1. Creates a 'labctl' system user
#   2. Creates a venv at /opt/labctl/venv and installs labctl into it
#   3. Sets up config in /var/lib/labctl
#   4. Installs and enables systemd services

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PROJECT_DIR/config/systemd"
LABCTL_HOME="/var/lib/labctl"
LABCTL_CONFIG="$LABCTL_HOME/.config/labctl"
LABCTL_VENV="/opt/labctl/venv"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Error: Please run as root (sudo)"
    exit 1
fi

echo "=== labctl Service Installation ==="
echo ""

# 1. Create labctl system user
if id -u labctl >/dev/null 2>&1; then
    echo "[ok] labctl user already exists"
else
    echo "[+] Creating labctl system user..."
    useradd --system --home-dir "$LABCTL_HOME" --create-home --shell /usr/sbin/nologin labctl
fi

# Add labctl to dialout group for serial device access
if id -nG labctl | grep -qw dialout; then
    echo "[ok] labctl user already in dialout group"
else
    echo "[+] Adding labctl to dialout group..."
    usermod -aG dialout labctl
fi

# 2. Create venv and install labctl
echo "[+] Creating virtual environment at $LABCTL_VENV..."
mkdir -p /opt/labctl
python3 -m venv "$LABCTL_VENV"

echo "[+] Installing labctl into venv..."
"$LABCTL_VENV/bin/pip" install --upgrade pip
"$LABCTL_VENV/bin/pip" install "$PROJECT_DIR[web]"

# Verify install
if [ ! -x "$LABCTL_VENV/bin/labctl" ]; then
    echo "Error: labctl not found in venv after install"
    exit 1
fi
echo "[ok] labctl installed at $LABCTL_VENV/bin/labctl"

# Symlink labctl into PATH so any user can run it
if [ -L /usr/local/bin/labctl ]; then
    echo "[ok] /usr/local/bin/labctl symlink already exists"
elif [ -e /usr/local/bin/labctl ]; then
    echo "[!] /usr/local/bin/labctl exists but is not a symlink, skipping"
else
    ln -s "$LABCTL_VENV/bin/labctl" /usr/local/bin/labctl
    echo "[ok] Symlinked labctl to /usr/local/bin/labctl"
fi

# 3. Set up config directory
echo "[+] Setting up config in $LABCTL_CONFIG..."
mkdir -p "$LABCTL_CONFIG"

if [ -f "$LABCTL_CONFIG/config.yaml" ]; then
    echo "[ok] config.yaml already exists, skipping"
else
    cp "$PROJECT_DIR/config/labctl.yaml.example" "$LABCTL_CONFIG/config.yaml"
    echo "[ok] Copied example config to $LABCTL_CONFIG/config.yaml"
fi

chown -R labctl:labctl "$LABCTL_HOME"

# 4. Install systemd service files
echo "[+] Installing systemd service files..."
cp "$CONFIG_DIR/labctl-monitor.service" /etc/systemd/system/
cp "$CONFIG_DIR/labctl-web.service" /etc/systemd/system/

systemctl daemon-reload

# 5. Enable and start services
echo "[+] Enabling services..."
systemctl enable labctl-monitor labctl-web

echo "[+] Starting services..."
systemctl start labctl-monitor labctl-web

echo ""
echo "=== Installation Complete ==="
echo ""

# Show status
systemctl --no-pager status labctl-monitor labctl-web || true

echo ""
echo "Useful commands:"
echo "  sudo systemctl status labctl-monitor labctl-web"
echo "  journalctl -u labctl-monitor -f"
echo "  journalctl -u labctl-web -f"
