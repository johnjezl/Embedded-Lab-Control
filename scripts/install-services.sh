#!/bin/bash
# Install labctl systemd services
#
# Usage: sudo ./install-services.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$SCRIPT_DIR/../config/systemd"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Create labctl user if needed
if ! id -u labctl >/dev/null 2>&1; then
    echo "Creating labctl user..."
    useradd -r -s /bin/false -d /var/lib/labctl labctl
    mkdir -p /var/lib/labctl
    chown labctl:labctl /var/lib/labctl
fi

# Install service files
echo "Installing systemd service files..."
cp "$CONFIG_DIR/labctl-monitor.service" /etc/systemd/system/
cp "$CONFIG_DIR/labctl-web.service" /etc/systemd/system/

# Reload systemd
echo "Reloading systemd..."
systemctl daemon-reload

echo ""
echo "Services installed. To enable and start:"
echo ""
echo "  # Enable on boot:"
echo "  sudo systemctl enable labctl-monitor"
echo "  sudo systemctl enable labctl-web"
echo ""
echo "  # Start now:"
echo "  sudo systemctl start labctl-monitor"
echo "  sudo systemctl start labctl-web"
echo ""
echo "  # Check status:"
echo "  sudo systemctl status labctl-monitor"
echo "  sudo systemctl status labctl-web"
echo ""
echo "  # View logs:"
echo "  journalctl -u labctl-monitor -f"
echo "  journalctl -u labctl-web -f"
