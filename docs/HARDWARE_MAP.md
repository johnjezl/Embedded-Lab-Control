# Mapping USB Serial Adapters

Linux names USB serial adapters `/dev/ttyUSB0`, `/dev/ttyACM0`, ... in the
order it finds them, so the names can change after a reboot or a replug.
labctl names each adapter by the **physical USB port** it is plugged into
instead: you register the adapter under a name, labctl generates udev
rules, and udev creates a stable `/dev/lab/<name>` symlink for it. Boards'
console ports then point at those symlinks.

This guide walks through that flow. Port paths, adapter names and board
names below are placeholders; use the values from your own machine.

## How USB paths work

The kernel identifies a USB device by its port chain:
`<bus>-<port>[.<port>...]`. For example:

```
Host controller, bus 1
└── Port 2: USB hub                    1-2
    ├── Port 1: USB serial adapter     1-2.1
    ├── Port 3: USB serial adapter     1-2.3
    └── Port 4: USB hub                1-2.4
        └── Port 1: USB serial adapter 1-2.4.1
```

The path stays the same as long as the adapter stays in the same hub port
(and the hub in the same host port). Moving an adapter to another port
changes its path; plugging a different adapter into the same port gives
the new one the old name.

Things to know:

- **Use a hub.** `labctl serial discover` only lists adapters behind a
  hub (paths with a dot, like `1-2.3`); an adapter plugged straight into a
  host port (`1-4`) is skipped. You can still register one by hand with
  the path from `udevadm` (see below).
- **One name per USB device.** The rule matches the device's path, so an
  adapter with several serial ports on one USB device (e.g. FT2232/FT4232)
  can't give each port its own name.
- **Serial numbers don't help much.** Many cheap adapters share a serial
  number (CP2102 units often report `0001`) or have none (CH340), which is
  why labctl matches on the port, not the serial number.

## 1. Find your adapters

Plug the adapters in, then:

```bash
labctl serial discover
```

```
DEVICE       USB PATH        VENDOR               MODEL                     REGISTERED
-------------------------------------------------------------------------------------
ttyUSB0      1-2.1           Silicon Labs         CP2102 USB to UART Brid   -
ttyUSB1      1-2.3           1a86                 USB Serial                -
ttyACM0      1-2.4.1         STMicroelectronics   STM32 STLink              -
```

`labctl serial discover -j` prints the same as JSON (with the serial
number). If you have several identical adapters, unplug and replug one at
a time to see which path is which, and label the hub ports.

Without labctl, `udevadm` shows the same path: the first `KERNELS=="..."`
line of the form `<bus>-<port>...` (no `:`) in

```bash
udevadm info --attribute-walk --name /dev/ttyUSB0 | grep KERNELS
```

## 2. Register each adapter

```bash
labctl serial add <name> <usb-path> [--vendor TEXT] [--model TEXT] [--serial-number TEXT]
```

For example:

```bash
labctl serial add board1-console 1-2.1 --vendor Silicon_Labs --model CP2102
labctl serial add board2-console 1-2.3
labctl serial add debug-probe    1-2.4.1
```

The name becomes the symlink `/dev/lab/<name>`. Vendor and model label the
adapter in `labctl serial list` and the rule comments, and the serial
number is stored for reference; only the USB path is matched. `labctl serial list` shows what is registered and
which board uses each adapter.

## 3. Generate and install the udev rules

Print the rules to check them:

```bash
labctl serial udev
```

Each adapter gets one rule, for example:

```
# board1-console (Silicon_Labs, CP2102)
SUBSYSTEM=="tty", KERNELS=="1-2.1", SYMLINK+="lab/board1-console", GROUP="dialout", MODE="0660"
```

Then install them as `/etc/udev/rules.d/99-lab-serial.rules` and reload
udev, either:

- **with write access to the rules file** (a system install made by
  `scripts/install-services.sh` makes it writable by the `labctl` group):

  ```bash
  labctl serial udev --install --reload
  ```

  The reload runs `udevadm` directly and falls back to `sudo udevadm`.
  With `--install`, labctl refuses to remove or re-point a `/dev/lab` link
  that a board claimed by someone else uses (`--override-claim` to
  proceed).

- **or with sudo**, from a file:

  ```bash
  labctl serial udev > 99-lab-serial.rules
  cat 99-lab-serial.rules        # rules, not "No serial devices registered"
  sudo install -m 644 99-lab-serial.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
  ```

  `scripts/install-udev.sh 99-lab-serial.rules` (from a git checkout) does
  the same, keeps a backup of the old file and lists `/dev/lab`
  afterwards. Neither checks claims the way `--install` does: before
  replacing the rules by hand, check `labctl claims list` for boards whose
  links change. On a system install, prefer `--install` above: both of these
  leave the file `0644` (`sudo install` also makes it `root:root`), so the
  `labctl` group can no longer update it with `--install`.

The devices are `root:dialout 0660`; to open them directly (relays from
the CLI, or `labctl console` on a port without ser2net), join `dialout`:
`sudo usermod -aG dialout $USER`, then log in again.

## 4. Assign adapters to boards

```bash
labctl port assign <board> console --serial-device board1-console
```

`--serial-device` sets the device path to `/dev/lab/<name>` and links the
port to the registered adapter. Port types are `console`, `jtag` and
`debug`. To serve the port over TCP, regenerate the ser2net config:

```bash
labctl ser2net generate --install
labctl ser2net reload
```

`labctl ports` lists the `/dev/lab` symlinks with their TCP ports.

## Changing the setup

- **Renamed an adapter:** `labctl serial rename <old> <new>`, then
  regenerate and reinstall the rules (step 3) and update the boards' port
  assignments.
- **Moved an adapter to another port:** the registration still has the
  old path. `labctl port remove <board> <type>` for each port using it
  (labctl won't remove an adapter in use), `labctl serial remove <name>`,
  `labctl serial add <name> <new-path>`, then steps 3 and 4 again. Both
  removals ask for confirmation; pass `--yes` from a script.
- **Port rows with no adapter link** (assigned by path without
  `--serial-device`): `labctl serial repair` shows the fix;
  `labctl serial repair --apply` writes it.

## Verification and troubleshooting

Check the links:

```bash
ls -l /dev/lab/
labctl ports --all     # --all also lists ttys no link points at
```

### Symlinks don't appear

1. Is the rules file installed and does it contain your adapters?
   `cat /etc/udev/rules.d/99-lab-serial.rules`
2. Reload and re-trigger:
   `sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty`
3. Does the rule match the device? `udevadm test $(udevadm info -q path -n /dev/ttyUSB0)`
   shows the rules applied and the links created.
4. Compare the registered path (`labctl serial list`) with the device's
   (`labctl serial discover`); a hub moved to another host port changes
   every path behind it.

### A link points at the wrong adapter

The adapters were swapped between hub ports, or the registration has the
wrong path. Check with `labctl serial discover` (the REGISTERED column
shows the name each path is registered under) and fix the registration as
in [Changing the setup](#changing-the-setup).

### Permission denied opening `/dev/lab/<name>`

Check `ls -lL /dev/lab/<name>` shows `root dialout ... crw-rw----` and
that you are in `dialout` (`id`). Users outside `dialout` can still reach
consoles over ser2net (`labctl console <board>`).

## Legacy: mapping file

Before adapters were kept in labctl's database, rules were generated from
a YAML mapping file. `scripts/generate-udev-rules.py` (see
`config/udev/port-mapping.yaml.example`) and `scripts/install-udev.sh`
still exist in the git checkout for that, but the flow above replaces
them.
