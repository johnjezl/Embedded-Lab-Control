# Bug: ser2net generate omits /dev/lab/ prefix for some ports

**Date:** 2026-04-09
**Severity:** Medium — serial connection silently fails until manually fixed
**Affected command:** `labctl ser2net generate`
**Status:** RESOLVED in commit 9aaba39 (2026-04-09)

## Resolution

Fixed in `src/labctl/serial/ser2net.py`: `generate_ser2net_config()` and
`Ser2NetPort.to_ser2net_dict()` now prefix bare device names with
`/dev/lab/`. Absolute paths pass through unchanged. Tests added in
`tests/unit/test_ser2net.py`.

## Summary

`labctl ser2net generate` produces a connector path without the `/dev/lab/` prefix for some serial ports, causing ser2net to fail with "Device open failure: Value or file not found" when a client connects.

## Steps to Reproduce

1. Add an SBC with a serial port assignment:
   ```
   labctl add jetson-nano-1
   labctl serial add port-2-3 --usb-path 1-12.1.3
   labctl assign-serial-port jetson-nano-1 console port-2-3
   ```
2. Install udev rules and regenerate ser2net config:
   ```
   sudo labctl serial udev --install --reload
   labctl ser2net generate | sudo tee /etc/ser2net.yaml
   sudo labctl ser2net reload
   ```
3. Attempt serial capture:
   ```
   labctl serial capture jetson-nano-1 --timeout 3
   ```

## Expected

```yaml
connection: &jetson-nano-1-console
  accepter: tcp,localhost,4007
  connector: serialdev,/dev/lab/port-2-3,115200n81,local
```

## Actual

```yaml
connection: &jetson-nano-1-console
  accepter: tcp,localhost,4007
  connector: serialdev,port-2-3,115200n81,local
```

The `/dev/lab/` prefix is missing. Other ports in the same config file have the correct prefix.

## Impact

- `serial capture` connects to ser2net but immediately gets "Device open failure"
- From the user's perspective, the connection "immediately returns" with no useful error
- `labctl serial udev --install` and restarting ser2net do not fix it since the config itself is wrong

## Workaround

Manually edit `/etc/ser2net.yaml` to add the `/dev/lab/` prefix:

```bash
sudo sed -i 's|serialdev,port-2-3,|serialdev,/dev/lab/port-2-3,|' /etc/ser2net.yaml
sudo labctl ser2net reload
```

## Notes

- The other three ports in the config (port-2-1, port-2-2, port-2-9) all have the correct `/dev/lab/` prefix
- port-2-3 was the most recently added serial device — the bug may be related to the add/registration order or a missing field in the database
- The health check correctly reports `serial: false` for the affected SBC
