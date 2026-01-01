# Hardware Map

This document describes the physical USB topology and port assignments for the lab.

## USB Hub Topology

```
Host PC (USB 3.0)
└── Port 10: 7-Port USB Hub (Genesys Logic GL3523)
    ├── Port 1: 4-Port USB Hub (internal)
    │   ├── Port 2: ST-Link V2.1 (ttyACM0) → /dev/lab/stlink-debug
    │   ├── Port 3: CP2102 USB-TTL (ttyUSB0) → /dev/lab/sbc1-console
    │   └── Port 4: CP2102 USB-TTL (ttyUSB1) → /dev/lab/sbc2-console
    ├── Port 3: CH340 USB-TTL (ttyUSB2) → /dev/lab/dev-serial1
    └── Port 4: CH340 USB-TTL (ttyUSB3) → /dev/lab/dev-serial2
```

## Port Assignments

| USB Path   | Symlink              | Device Type     | Assigned To      |
|------------|----------------------|-----------------|------------------|
| 1-10.1.2   | /dev/lab/stlink-debug| ST-Link V2.1   | Debug probe      |
| 1-10.1.3   | /dev/lab/sbc1-console| CP2102 USB-TTL | SBC 1 console    |
| 1-10.1.4   | /dev/lab/sbc2-console| CP2102 USB-TTL | SBC 2 console    |
| 1-10.3     | /dev/lab/dev-serial1 | CH340 USB-TTL  | General use      |
| 1-10.4     | /dev/lab/dev-serial2 | CH340 USB-TTL  | General use      |

## Device Details

### ST-Link V2.1
- **Vendor**: STMicroelectronics (0483:3752)
- **Serial**: 066EFF534871754867213717
- **Notes**: Built-in USB CDC ACM interface for debug UART

### CP2102 USB-TTL Adapters
- **Vendor**: Silicon Labs (10c4:ea60)
- **Serial**: 0001 (non-unique - use physical path)
- **Notes**: Common serial adapters, identical serials require path-based identification

### CH340 USB-TTL Adapters
- **Vendor**: QinHeng Electronics (1a86:7523)
- **Serial**: None
- **Notes**: Budget serial adapters, no serial number

## Updating Port Assignments

1. Discover connected devices:
   ```bash
   ./scripts/discover-usb-serial.sh
   ```

2. Edit the mapping file:
   ```bash
   vim config/udev/port-mapping.yaml
   ```

3. Regenerate and install rules:
   ```bash
   source .venv/bin/activate
   python scripts/generate-udev-rules.py \
       --mapping config/udev/port-mapping.yaml \
       --output config/udev/99-lab-serial.rules
   sudo ./scripts/install-udev.sh
   ```

## Troubleshooting

### Symlinks not appearing
1. Check udev rules are installed: `ls -la /etc/udev/rules.d/99-lab-serial.rules`
2. Reload rules: `sudo udevadm control --reload-rules`
3. Trigger rules: `sudo udevadm trigger --subsystem-match=tty`
4. Check udev debug: `udevadm test /sys/class/tty/ttyUSB0`

### Device assigned wrong name
1. Verify USB path: `udevadm info -q property -n /dev/ttyUSBx | grep DEVPATH`
2. Update mapping in `config/udev/port-mapping.yaml`
3. Regenerate and reinstall rules
