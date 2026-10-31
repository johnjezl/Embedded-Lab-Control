#!/bin/bash
#
# install-udev.sh - Install a udev rules file for lab serial devices
#
# This script copies a generated udev rules file to /etc/udev/rules.d/,
# reloads the udev daemon, and triggers the rules to create symlinks.
#
# Usage:
#   sudo ./install-udev.sh RULES_FILE
#
# Generate RULES_FILE from the adapters registered with labctl:
#   labctl serial udev > 99-lab-serial.rules
# (or, legacy, from a mapping file with scripts/generate-udev-rules.py).
# `labctl serial udev --install --reload` installs the rules itself when
# you can write /etc/udev/rules.d/99-lab-serial.rules; this script is
# for installing a file you have generated and checked.

set -e

DEST_DIR="/etc/udev/rules.d"
DEST_FILE="$DEST_DIR/99-lab-serial.rules"

usage() {
    echo "Usage: sudo $0 RULES_FILE" >&2
    echo "" >&2
    echo "Generate a rules file first, from the adapters registered with labctl:" >&2
    echo "  labctl serial add <name> <usb-path>   # for each adapter" >&2
    echo "  labctl serial udev > 99-lab-serial.rules" >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 1
fi
RULES_FILE="$1"

if [[ ! -f "$RULES_FILE" ]]; then
    echo "Error: Rules file not found: $RULES_FILE" >&2
    echo "" >&2
    usage
    exit 1
fi

# `labctl serial udev` prints a message instead of rules when no adapters
# are registered; don't install that. Require at least one SYMLINK
# assignment (the pattern labctl's own parser uses, `_RULE_SYMLINK_RE` in
# labctl/serial/udev.py), and every other non-comment line to start like
# a udev rule (KEY, optional {attr}, operator).
if ! grep -qE 'SYMLINK[[:space:]]*\+?=[[:space:]]*"' "$RULES_FILE"; then
    echo "Error: $RULES_FILE contains no udev rules (no SYMLINK assignment)" >&2
    exit 1
fi
BAD_LINE=$(grep -nvE '^[[:space:]]*(#.*)?$|^[[:space:]]*[A-Za-z_]+(\{[^}]*\})?[[:space:]]*(==|!=|\+=|-=|:=|=)' "$RULES_FILE" | head -n 1 || true)
if [[ -n "$BAD_LINE" ]]; then
    echo "Error: $RULES_FILE has a line that isn't a udev rule:" >&2
    echo "  $BAD_LINE" >&2
    exit 1
fi

# Check for root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (use sudo)" >&2
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
