---
name: deploy-and-test
description: Build, deploy to hardware via SDWire, boot, capture serial output, and analyze results. Use when the user says "deploy", "test on hardware", "flash and boot", "deploy and test", or invokes /deploy-and-test.
argument-hint: "[sbc-name] [--runs N]"
---

# Deploy and Test Skill

Orchestrate the full embedded development inner loop: build, deploy to hardware, boot, capture serial output, and analyze results.

You call labctl MCP tools for all hardware operations. You do NOT interact with hardware directly.

To use this skill, copy it to `.claude/skills/deploy-and-test/SKILL.md` in your project (or `~/.claude/skills/deploy-and-test/SKILL.md` for all projects).

### Confirming destructive tools

`sdwire_update`, `power_cycle` and `boot_test` are destructive. By default (`mcp.confirm_destructive`) the first call does nothing: it returns `"status": "confirmation_required"` with a plan and a `confirm_token`. Check the plan, then call the same tool again with exactly the same arguments plus `confirm_token=<token>`. The token is single-use and expires after 120 seconds. In the steps below, "call X" means both calls whenever the first one asks for confirmation.

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

## Step 0b: Claim the board

Call `claim_sbc(sbc_name=<sbc>, duration_minutes=<enough for the run>, reason="deploy-and-test: <what you are testing>")` before touching the hardware. When the lab sets `claims.require_claim`, mutating tools refuse an unclaimed board. In every case the claim stops other agents from power-cycling the board mid-test. If another agent holds it, report who and stop; don't override. When you finish (pass or fail), call `release_sbc(sbc_name=<sbc>)`.

## Step 0c: Make the binary reachable by the MCP server

The MCP server runs on the lab host and cannot read paths on your machine, and it never accepts a relative path like `build/kernel/slmos.bin`. Use one of these:

- **Upload it** (HTTP transport; needs `mcp.upload_dir` on the server). Use the same API key as the MCP connection:
  ```bash
  curl --fail-with-body -T <binary> -H "Authorization: Bearer $LABCTL_API_KEY" \
    "<mcp-url>/images/<name>?sha256=$(sha256sum <binary> | cut -d' ' -f1)"
  ```
  `<mcp-url>` is the base of the MCP server's URL (its `/mcp` endpoint without `/mcp`): `http://127.0.0.1:8080` on the lab host with the shipped service, or `https://<lab-host>:<port>` when TLS and a non-loopback `--host` are set up.
  Then pass the bare `<name>` (no `/`) as the source. `list_images` shows what is uploaded.
- **Absolute host path** (when the server runs on the same machine as the build): pass the binary's absolute path. It must be inside a directory listed in `mcp.allowed_read_paths`.

Below, `<image>` means that bare name or absolute path.

## Step 1: Build

Run the build command via Bash. If it fails, report the error with the relevant compiler output and stop.

Report: `Build: <command> ... OK (Xs)` or `Build: FAILED`

## Step 2: Deploy

Call the `sdwire_update` MCP tool:
```
sdwire_update(sbc_name=<sbc>, partition=<N>, copies=["<image>:<sd_dest>"])
```

This powers the board off before switching the card to the host. Without `reboot=true` the board stays off until Step 3.

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
boot_test(sbc_name=<sbc>, expect_pattern=<pattern>, runs=<N>, timeout=<boot_timeout>, image=<image>, dest=<sd_dest>, partition=<partition>)
```

The `boot_test` tool handles deploy, repeated power cycling, and serial capture internally. Report its output directly.

If `boot_test` is not available, loop Steps 2-4 manually N times and report aggregate results.

## Error Handling

- **Build failure**: Show the last 30 lines of build output. Do not proceed to deploy.
- **Deploy failure**: Report the MCP tool error. Common causes: SDWire not assigned, block device not found, SD card not in host mode, a source path outside `mcp.allowed_read_paths`, or an image name that was never uploaded.
- **Claim refused**: Another agent holds the board. Report the holder and stop. Optionally call `request_sbc_release(sbc_name=<sbc>, reason=...)` to ask them to release it.
- **Confirmation required**: This is not an error. Repeat the call with the returned `confirm_token`.
- **Boot timeout**: Report the last few lines of captured output (helps diagnose where boot stalled). Mark as FAIL.
- **No serial port**: If the SBC has no console port configured, warn the user but still deploy. Skip capture step.
- **MCP tool not available**: If an MCP tool is not connected, tell the user which labctl MCP tools are required and how to connect them.
