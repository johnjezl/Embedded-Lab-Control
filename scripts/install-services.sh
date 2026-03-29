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

# 4. Set up udev rules file (group-writable so labctl users don't need sudo)
echo "[+] Setting up udev rules file..."
touch /etc/udev/rules.d/99-lab-serial.rules
chown root:labctl /etc/udev/rules.d/99-lab-serial.rules
chmod 664 /etc/udev/rules.d/99-lab-serial.rules
echo "[ok] /etc/udev/rules.d/99-lab-serial.rules (group-writable by labctl)"

# Allow labctl group to reload udev without password
# Make ser2net config group-writable
if [ -f /etc/ser2net.yaml ]; then
    chown root:labctl /etc/ser2net.yaml
    chmod 664 /etc/ser2net.yaml
    echo "[ok] /etc/ser2net.yaml (group-writable by labctl)"
fi

# Allow labctl group to reload udev and ser2net without password
SUDOERS_FILE="/etc/sudoers.d/labctl"
cat > "$SUDOERS_FILE" << 'SUDOERS'
%labctl ALL=(ALL) NOPASSWD: /usr/bin/udevadm
%labctl ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ser2net
SUDOERS
chmod 440 "$SUDOERS_FILE"
echo "[ok] Added passwordless sudo for udevadm and ser2net restart (labctl group)"

# 5. Install systemd service files
echo "[+] Installing systemd service files..."
cp "$CONFIG_DIR/labctl-monitor.service" /etc/systemd/system/
cp "$CONFIG_DIR/labctl-web.service" /etc/systemd/system/
cp "$CONFIG_DIR/labctl-mcp.service" /etc/systemd/system/

systemctl daemon-reload

# 6. Enable and start services
echo "[+] Enabling services..."
systemctl enable labctl-monitor labctl-web

echo "[+] Starting services..."
systemctl start labctl-monitor labctl-web

# MCP service is installed but not enabled by default
echo "[ok] MCP service installed (not enabled by default)"
echo "     To enable: sudo systemctl enable --now labctl-mcp"

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
