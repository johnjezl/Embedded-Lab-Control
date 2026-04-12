# Feature Spec: SDWire Power Safety Interlock

**Status:** IMPLEMENTED in commit 9aaba39 (2026-04-09)

## Resolution

- `sdwire_to_host` MCP tool and `labctl sdwire host` CLI check power state
  and reject if SBC is powered on. Override via `force=True` / `--force`.
- `sdwire_update` MCP tool and `labctl sdwire update` CLI auto power-off
  before switching to host.
- `flash_image` already powers off before switching (prior commit).
- Best-effort: allows operation when power state is unreachable/unknown.
- Tests in `tests/unit/test_mcp_server.py::TestMcpSDWireTools`.

## Problem

Switching an SDWire to host mode while the SBC is powered on causes SD card bus contention. The host sees the card as 0B (no capacity), the USB storage device enters a reset loop, and the card becomes inaccessible until the SBC is powered off and the SDWire is re-toggled.

This was observed on 2026-04-09 when `sdwire_to_host` was called for `pi-5-1` while the Pi 5 was powered on. The SD card (`sdc`) showed 0B in `lsblk`, the USB mass storage driver entered a continuous reset cycle visible in `dmesg`, and `sdwire_update` failed repeatedly with `"Error: Cannot determine block device."` Recovery required powering off the SBC, physically reseating the SDWire, and re-toggling DUT/host mode.

## Proposed Behavior

### `sdwire_to_host`

Reject the command if the SBC is powered on:

```
$ labctl sdwire-to-host pi-5-1
Error: pi-5-1 is powered on. Power off before switching SD to host mode.
Use --force to override (risks SD card corruption).
```

- Check power state before switching.
- If powered on, return an error with an explanation.
- Provide `--force` flag (CLI) or `force: true` parameter (MCP) for cases where the user knows what they're doing (e.g., SBC is halted but power relay is still on).

### `sdwire_update`

Auto power-off at the start of the operation:

```
$ labctl sdwire-update pi-5-1 --partition 1 --copy slmos.bin:kernel_2712.img --reboot
Powering off pi-5-1...
SD card switched to host: pi-5-1
Partition 1: Copied: kernel_2712.img.
SD card switched to DUT: pi-5-1
Power cycled pi-5-1.
```

- `sdwire_update` is an atomic high-level operation with clear intent (flash and optionally reboot). Auto power-off is safe and expected here.
- If `--reboot` is specified, the user clearly expects a power cycle anyway.
- If `--reboot` is not specified, still power off before switching to host, then leave the SBC off after switching back to DUT.

### `sdwire_to_dut`

No change needed. Switching to DUT mode is safe regardless of power state — the SBC will simply start reading from the card on next boot.

## MCP Tool Parameter Changes

### `sdwire_to_host`

Add optional `force` parameter (default: `false`):

```python
async def sdwire_to_host(sbc_name: str, force: bool = False) -> str:
    if not force:
        power_state = await get_power_state(sbc_name)
        if power_state == "on":
            return "Error: {sbc_name} is powered on. Power off before switching SD to host mode. Use force=true to override."
    # ... existing switch logic
```

### `sdwire_update`

No parameter changes. Add power-off step at the beginning of the operation, before switching to host mode.

## Edge Cases

- **Power state unknown:** If the power controller is unreachable or the SBC has no power control configured, log a warning but allow the operation. The interlock is best-effort.
- **SBC halted but power on:** Some SBCs may be halted (OS shut down) but the power relay is still on. The `--force` flag covers this case.
- **Race condition:** The SBC could be powered on between the check and the switch. This is unlikely in a single-user lab and not worth solving with locking.

## Testing

- Verify `sdwire_to_host` rejects when SBC is powered on.
- Verify `sdwire_to_host --force` succeeds when SBC is powered on.
- Verify `sdwire_to_host` succeeds when SBC is powered off.
- Verify `sdwire_update` powers off SBC before switching to host.
- Verify `sdwire_update` with `--reboot` powers off, flashes, switches to DUT, then power cycles.
- Verify behavior when power state is unknown (no power controller configured).
