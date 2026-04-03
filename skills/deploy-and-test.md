---
name: deploy-and-test
description: Build, deploy to hardware via SDWire, boot, capture serial output, and analyze results. Use when the user says "deploy", "test on hardware", "flash and boot", "deploy and test", or invokes /deploy-and-test.
argument-hint: "[sbc-name] [--runs N]"
---

# Deploy and Test Skill

Orchestrate the full embedded development inner loop: build, deploy to hardware, boot, capture serial output, and analyze results.

You call labctl MCP tools for all hardware operations. You do NOT interact with hardware directly.

## Step 0: Resolve Configuration

Determine the deploy configuration from these sources, in priority order:

1. **Arguments**: The user may specify the target SBC name and/or `--runs N` in `$ARGUMENTS`
2. **Deploy config file**: Read `.claude/deploy-config.json` if it exists. Format:
   ```json
   {
     "targets": {
       "sbc-name": {
         "build_command": "make kernel PLATFORM=RASPI5",
         "binary": "build/kernel/slmos.bin",
         "sd_dest": "kernel_2712.img",
         "partition": 1,
         "serial_port": "pi-5-1-console",
         "success_pattern": "slmos>",
         "boot_timeout": 30
       }
     },
     "default_target": "sbc-name"
   }
   ```
3. **CLAUDE.md / project docs**: Look for build commands, output paths, deploy targets
4. **Ask the user**: For anything not found above

You need all of these to proceed:
- `build_command` — how to build (e.g., `make kernel PLATFORM=RASPI5`)
- `binary` — path to the built artifact
- `sbc_name` — target SBC name in labctl
- `sd_dest` — destination filename on the SD card
- `partition` — SD card partition number (typically 1)
- `success_pattern` — regex to match in serial output for success
- `boot_timeout` — seconds to wait for boot (default: 30)

## Step 1: Build

Run the build command via Bash. If it fails, report the error with the relevant compiler output and stop.

Report: `Build: <command> ... OK (Xs)` or `Build: FAILED`

## Step 2: Deploy

Call the `sdwire_update` MCP tool:
```
sdwire_update(sbc_name=<sbc>, partition=<N>, copies=["<binary>:<sd_dest>"])
```

If deploy fails, report the error and stop.

Report: `Deploy: <binary> -> <sd_dest> (partition <N>) ... OK`

## Step 3: Boot and Capture

Power cycle the SBC and capture serial output.

Call the `power_cycle` MCP tool:
```
power_cycle(sbc_name=<sbc>)
```

Then immediately call `serial_capture`:
```
serial_capture(port_name=<sbc_name or serial_port>, timeout=<boot_timeout>, until_pattern=<success_pattern>)
```

If `serial_capture` is not available, inform the user to check the serial console manually.

Report: `Boot: Waiting for '<pattern>' ...`

## Step 4: Analyze and Report

Parse the captured serial output and report:

- **Success/failure**: Did the success pattern appear?
- **Boot time**: How long until the pattern matched (from serial_capture elapsed time)
- **Errors**: Look for lines containing `[ERROR]`, `[FAIL]`, `panic`, `fault`, `exception` (case-insensitive)
- **Warnings**: Look for lines containing `[WARN]`, `warning` (case-insensitive)
- **Summary**: Brief assessment

Format the report like this:
```
Deploy & Test: <sbc_name>
------------------------------
Build:   <command> ... OK (Xs)
Deploy:  <binary> -> <sd_dest> (partition N) ... OK
Boot:    Waiting for '<pattern>' ...

Result:  PASS (Xs to pattern match)

Warnings:
  - <any warning lines from output>

Errors:
  - <any error lines from output>
  (or "No errors detected.")
```

## Boot Reliability Testing Mode

If the user specifies `--runs N` in arguments or asks for "boot reliability" / "test N times":

Call the `boot_test` MCP tool:
```
boot_test(sbc_name=<sbc>, expect_pattern=<pattern>, runs=<N>, timeout=<boot_timeout>, image=<binary>, dest=<sd_dest>, partition=<partition>)
```

The `boot_test` tool handles deploy, repeated power cycling, and serial capture internally. Report its output directly.

If `boot_test` is not available, loop Steps 2-4 manually N times and report aggregate results.

## Error Handling

- **Build failure**: Show the last 30 lines of build output. Do not proceed to deploy.
- **Deploy failure**: Report the MCP tool error. Common causes: SDWire not assigned, block device not found, SD card not in host mode.
- **Boot timeout**: Report the last few lines of captured output (helps diagnose where boot stalled). Mark as FAIL.
- **No serial port**: If the SBC has no console port configured, warn the user but still deploy. Skip capture step.
- **MCP tool not available**: If an MCP tool is not connected, tell the user which labctl MCP tools are required and how to connect them.
