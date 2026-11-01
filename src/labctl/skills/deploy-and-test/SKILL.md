---
name: deploy-and-test
description: Build, deploy to hardware via SDWire, boot, capture serial output, and analyze results. Use when the user says "deploy", "test on hardware", "flash and boot", "deploy and test", or invokes /deploy-and-test.
argument-hint: "[sbc-name] [--runs N]"
---

# Deploy and Test Skill

Orchestrate the full embedded development inner loop: build, deploy to hardware, boot, capture serial output, and analyze results.

You call labctl MCP tools for all hardware operations. You do NOT interact with hardware directly.

To use this skill, install it with `labctl skill install deploy-and-test` in your project (or `labctl skill install deploy-and-test --user` for all your projects).

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

## Step 0b: Make the binary reachable by the MCP server

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

## Step 1b: Claim the board

Claim the board after the build succeeds (and the binary is uploaded), right before the first hardware step: a failed build then never holds a board, and other agents can use it while you compile.

Call `claim_sbc(sbc_name=<sbc>, duration_minutes=<duration>, reason="deploy-and-test: <what you are testing>")`. When the lab sets `claims.require_claim`, the hardware tools refuse an unclaimed board; in every case the claim stops other agents from power-cycling it mid-test. If another agent holds it (`"error": "sbc_claimed"`), report who and stop; don't override. Optionally ask them with `request_sbc_release(sbc_name=<sbc>, reason=...)`.

**How long to claim.** Cover the whole run, including the gaps between your calls, within `claims.min_duration_minutes`..`max_duration_minutes`:

- single deploy and boot (Steps 2-4): about 10 minutes is plenty for most boards; add the time a large image takes to copy;
- `boot_test`: at least `runs × (boot_timeout + cycle delay + ~5 s)` plus the deploy, plus a few minutes' margin. With the default 3 s cycle delay, 10 runs of 30 s need about 7 minutes, so claim 15.

Long tool calls (`boot_test`, `serial_capture`, `sdwire_update`, `flash_image`) keep a claim you hold alive while they run, and every call on the board renews it. But a claim still expires during a long gap between calls (e.g. while you analyze output): call `renew_sbc_claim(sbc_name=<sbc>, duration_minutes=...)` before a step you expect to take long, or after a pause. Don't claim for much longer than you need: if your session ends without releasing the claim (an interrupted client, a crashed or killed server), the claim blocks other agents until it expires, plus the lab's grace period.

If a long call stops because of a claim (`boot_test` starts with `Stopped: <reason>` and reports `Result: INCOMPLETE` or `STOPPED before the first run`; `sdwire_update`/`flash_image` say `Not power cycled: ...`/`Not powered on: ...` when your claim was lost, or `<board> is left off ... because '<board>' is now claimed by '<agent>'`), your claim was released or someone else claimed the board or one sharing its hardware: report it, and that the board may be left off, and stop. Don't claim it again to carry on.

**Release on every path.** Call `release_sbc(sbc_name=<sbc>)` as soon as you are done with the board: after the report (pass or fail), and also when you stop early (deploy failure, boot timeout, a confirmation you decided not to give, or the user cancelling). If you are interrupted before you can release it, the claim ends by itself, but not always at once: a stdio MCP server that exits cleanly releases its session's claims; otherwise (an HTTP session, or a stdio server that crashed or was killed) the claim lasts until it expires and its grace period (`claims.grace_period_seconds`) has passed.

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

The `boot_test` tool handles deploy, repeated power cycling, and serial capture internally. Report its output directly. Claim the board for the whole test first (Step 1b sizes it); `boot_test` keeps that claim alive while it runs, and stops early (its result starts with `Stopped: <reason>`) if the claim is lost or someone else claims the board. Report a stopped test as incomplete, not as a pass rate.

If `boot_test` is not available, loop Steps 2-4 manually N times and report aggregate results, calling `renew_sbc_claim` between iterations if the loop may outlast the claim.

Release the claim when the test is done (Step 1b).

## Error Handling

In every case below that stops the workflow after Step 1b, call `release_sbc(sbc_name=<sbc>)` before you finish.

- **Build failure**: Show the last 30 lines of build output. Do not proceed to deploy. (The board isn't claimed yet.)
- **Deploy failure**: Report the MCP tool error and release the claim. Common causes: SDWire not assigned, block device not found, SD card not in host mode, a source path outside `mcp.allowed_read_paths`, or an image name that was never uploaded.
- **Claim refused**: Another agent holds the board. Report the holder and stop. Optionally call `request_sbc_release(sbc_name=<sbc>, reason=...)` to ask them to release it.
- **Claim lost or taken mid-run**: `boot_test` starts with `Stopped: <reason>`, or `sdwire_update`/`flash_image` say the board was left off (not power cycled or powered on) and why. Your claim was released, or someone else claimed the board or one sharing its hardware: report it (the board may be off) and stop; don't re-claim it to carry on.
- **Confirmation required**: This is not an error. Repeat the call with the returned `confirm_token`. If you decide not to confirm, release the claim.
- **Boot timeout**: Report the last few lines of captured output (helps diagnose where boot stalled). Mark as FAIL, and release the claim once you are done.
- **No serial port**: If the SBC has no console port configured, warn the user but still deploy. Skip capture step.
- **MCP tool not available**: If an MCP tool is not connected, tell the user which labctl MCP tools are required and how to connect them.
