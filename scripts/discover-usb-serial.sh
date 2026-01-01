#!/bin/bash
#
# discover-usb-serial.sh - Enumerate USB serial devices with physical port info
#
# This script discovers all connected USB serial devices (ttyUSB*, ttyACM*)
# and outputs their physical USB path, which can be used for deterministic
# udev rules.
#
# Usage: ./discover-usb-serial.sh [--json]
#
# Output format (default):
#   DEVICE    USB_PATH      VENDOR              MODEL
#   ttyUSB0   1-10.1.3      Silicon_Labs        CP2102_USB_to_UART_Bridge_Controller
#
# With --json flag, outputs JSON for programmatic use.

set -e

JSON_OUTPUT=false
if [[ "$1" == "--json" ]]; then
    JSON_OUTPUT=true
fi

# Find all ttyUSB and ttyACM devices
devices=$(find /dev -maxdepth 1 -name 'ttyUSB*' -o -name 'ttyACM*' 2>/dev/null | sort)

if [[ -z "$devices" ]]; then
    echo "No USB serial devices found." >&2
    exit 0
fi

if $JSON_OUTPUT; then
    echo "["
    first=true
fi

for dev in $devices; do
    devname=$(basename "$dev")

    # Get udev properties
    props=$(udevadm info -q property -n "$dev" 2>/dev/null)

    # Extract relevant fields
    devpath=$(echo "$props" | grep '^DEVPATH=' | cut -d= -f2)
    id_vendor=$(echo "$props" | grep '^ID_VENDOR=' | cut -d= -f2)
    id_model=$(echo "$props" | grep '^ID_MODEL=' | cut -d= -f2)
    id_serial=$(echo "$props" | grep '^ID_SERIAL_SHORT=' | cut -d= -f2)
    id_vendor_id=$(echo "$props" | grep '^ID_VENDOR_ID=' | cut -d= -f2)
    id_model_id=$(echo "$props" | grep '^ID_MODEL_ID=' | cut -d= -f2)

    # Extract the USB path from DEVPATH (e.g., 1-10.1.3 from the full path)
    # Look for the last USB device path component before the interface (:N.N)
    # Example DEVPATH: /devices/pci.../usb1/1-10/1-10.1/1-10.1.3/1-10.1.3:1.0/ttyUSB0/...
    # We want: 1-10.1.3
    kernels=$(echo "$devpath" | grep -oE '[0-9]+-[0-9]+(\.[0-9]+)*' | tail -1)

    if $JSON_OUTPUT; then
        if ! $first; then
            echo ","
        fi
        first=false
        cat <<EOF
  {
    "device": "$devname",
    "path": "/dev/$devname",
    "usb_path": "$kernels",
    "vendor": "$id_vendor",
    "vendor_id": "$id_vendor_id",
    "model": "$id_model",
    "model_id": "$id_model_id",
    "serial": "$id_serial"
  }
EOF
    else
        if [[ "$dev" == "$devices" || "$dev" == $(echo "$devices" | head -1) ]]; then
            printf "%-10s %-14s %-20s %-40s %s\n" "DEVICE" "USB_PATH" "VENDOR" "MODEL" "SERIAL"
            printf "%-10s %-14s %-20s %-40s %s\n" "------" "--------" "------" "-----" "------"
        fi
        printf "%-10s %-14s %-20s %-40s %s\n" "$devname" "$kernels" "$id_vendor" "$id_model" "${id_serial:--}"
    fi
done

if $JSON_OUTPUT; then
    echo ""
    echo "]"
fi
