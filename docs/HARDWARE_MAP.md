# Hardware Map

This document shows how to describe a lab's physical USB topology and
serial port assignments. **Everything below is an example**: hub
models, USB paths, names and serial numbers are placeholders. Record
your own lab's values in the same shape (or keep them only in labctl's
database).

## USB Hub Topology (example)

```
Host PC (USB 3.0)
└── Port 4: 7-Port USB Hub
    ├── Port 1: 4-Port USB Hub (internal)
    │   ├── Port 2: Debug probe with CDC ACM UART (ttyACM0) → /dev/lab/debug-probe
    │   ├── Port 3: CP2102 USB-TTL (ttyUSB0) → /dev/lab/sbc1-console
    │   └── Port 4: CP2102 USB-TTL (ttyUSB1) → /dev/lab/sbc2-console
    ├── Port 3: CH340 USB-TTL (ttyUSB2) → /dev/lab/dev-serial1
    └── Port 4: CH340 USB-TTL (ttyUSB3) → /dev/lab/dev-serial2
```

## Port Assignments (example)

| USB Path | Symlink               | Device Type     | Assigned To   |
|----------|-----------------------|-----------------|---------------|
| 1-4.1.2  | /dev/lab/debug-probe  | Debug probe     | Debug UART    |
| 1-4.1.3  | /dev/lab/sbc1-console | CP2102 USB-TTL  | SBC 1 console |
| 1-4.1.4  | /dev/lab/sbc2-console | CP2102 USB-TTL  | SBC 2 console |
| 1-4.3    | /dev/lab/dev-serial1  | CH340 USB-TTL   | General use   |
| 1-4.4    | /dev/lab/dev-serial2  | CH340 USB-TTL   | General use   |

The USB path (`<bus>-<port>.<port>…`) is the physical port chain the
kernel reports; it stays the same as long as the adapter stays in the
same hub port, which is why labctl names adapters by path rather than
by `/dev/ttyUSBn`.

## Device Details (common adapters)

### Debug probe with a CDC ACM UART (e.g. ST-Link V2.1)
- **Vendor**: STMicroelectronics (0483:3752)
- **Serial**: unique per probe (`<serial-number>`)
- **Notes**: Built-in USB CDC ACM interface for debug UART

### CP2102 USB-TTL Adapters
- **Vendor**: Silicon Labs (10c4:ea60)
- **Serial**: often `0001` on every unit (non-unique - use physical path)
- **Notes**: Common serial adapters, identical serials require path-based identification

### CH340 USB-TTL Adapters
- **Vendor**: QinHeng Electronics (1a86:7523)
- **Serial**: None
- **Notes**: Budget serial adapters, no serial number

## Updating Port Assignments

Adapters are registered in labctl's database and the udev rules are
generated from it:

1. Discover connected devices (shows each adapter's USB path):
   ```bash
   labctl serial discover
   ```

2. Register each adapter under a name (the symlink becomes
   `/dev/lab/<name>`):
   ```bash
   labctl serial add sbc1-console 1-4.1.3 --vendor Silicon_Labs --model CP2102
   ```

3. Generate, install and reload the udev rules
   (`/etc/udev/rules.d/99-lab-serial.rules`):
   ```bash
   labctl serial udev --install --reload
   ```
   Writing the rules needs write access to that file
   (`scripts/install-services.sh` makes it group-writable by `labctl`);
   the reload falls back to `sudo udevadm`. Without `--install`, the rules are printed instead.

4. Assign the port to an SBC:
   ```bash
   labctl port assign <sbc> console --serial-device sbc1-console
   ```

**Legacy:** before adapters were tracked in the database, rules were
generated from a mapping file and installed with:

```bash
scripts/generate-udev-rules.py --mapping config/udev/port-mapping.yaml \
    --output config/udev/99-lab-serial.rules
sudo scripts/install-udev.sh   # installs config/udev/99-lab-serial.rules
```

These scripts still exist but are not needed with the flow above.

## Troubleshooting

### Symlinks not appearing
1. Check udev rules are installed: `ls -la /etc/udev/rules.d/99-lab-serial.rules`
2. Reload rules: `sudo udevadm control --reload-rules`
3. Trigger rules: `sudo udevadm trigger --subsystem-match=tty`
4. Check udev debug: `udevadm test /sys/class/tty/ttyUSB0`

### Device assigned wrong name
1. Verify USB path: `udevadm info -q property -n /dev/ttyUSBx | grep DEVPATH`
2. Update the registration (`labctl serial remove` / `labctl serial add`)
3. Regenerate and reinstall rules (`labctl serial udev --install --reload`)
