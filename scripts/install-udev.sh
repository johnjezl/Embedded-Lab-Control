#!/bin/bash
#
# install-udev.sh - Install udev rules for lab serial devices
#
# This script copies the generated udev rules to /etc/udev/rules.d/,
# reloads the udev daemon, and triggers the rules to create symlinks.
#
# Usage:
#   sudo ./install-udev.sh [rules-file]
#
# If no rules file is specified, uses config/udev/99-lab-serial.rules

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DEFAULT_RULES="$PROJECT_DIR/config/udev/99-lab-serial.rules"
DEST_DIR="/etc/udev/rules.d"
DEST_FILE="$DEST_DIR/99-lab-serial.rules"

# Check for root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (use sudo)" >&2
    exit 1
fi

# Get rules file
RULES_FILE="${1:-$DEFAULT_RULES}"

if [[ ! -f "$RULES_FILE" ]]; then
    echo "Error: Rules file not found: $RULES_FILE" >&2
    echo "" >&2
    echo "Generate rules first with:" >&2
    echo "  ./scripts/generate-udev-rules.py --output config/udev/99-lab-serial.rules" >&2
    exit 1
fi

echo "Installing udev rules..."
echo "  Source: $RULES_FILE"
echo "  Destination: $DEST_FILE"

# Backup existing rules if present
if [[ -f "$DEST_FILE" ]]; then
    BACKUP="$DEST_FILE.bak.$(date +%Y%m%d%H%M%S)"
    echo "  Backing up existing rules to: $BACKUP"
    cp "$DEST_FILE" "$BACKUP"
fi

# Copy rules
cp "$RULES_FILE" "$DEST_FILE"
chmod 644 "$DEST_FILE"

echo ""
echo "Reloading udev rules..."
udevadm control --reload-rules

echo "Triggering udev to apply rules..."
udevadm trigger --subsystem-match=tty

# Wait a moment for symlinks to be created
sleep 1

echo ""
echo "Checking /dev/lab/ symlinks:"
if [[ -d /dev/lab ]]; then
    ls -la /dev/lab/ 2>/dev/null || echo "  (no symlinks yet)"
else
    echo "  /dev/lab/ directory not created yet"
    echo "  This is normal if no matching devices are connected"
fi

echo ""
echo "Done. Symlinks will be created automatically when devices are connected."
