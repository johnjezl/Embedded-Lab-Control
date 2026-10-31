# Project Status

## Current State

- **Milestone**: Public PyPI release (`embedded-lab-control`)
- **Sub-task**: P2 (PyPI release), PR E release prep in progress
  (lab-specific files removed, generic `HARDWARE_MAP.md`, `MANIFEST.in`,
  install-the-extra errors, README PyPI install, CI triggers); PR F
  (publish workflow, 0.2.0) next. Phase 0 and Phase 1 (safety) are done
- **Status**: `main` has everything merged through PR #43; deployed to
  tarrasque through #40 (#39/#40 on 2026-10-30). #42 and #43 (#41, the
  code gaps the docs audit found) are merged but not deployed. In
  progress before P2: #2 (long calls keep the caller's claim alive;
  `/deploy-and-test` claim handling).
  Phase 1 (WS1–WS5, PRs #13–#18), claim enforcement on every surface
  (D013, PRs #22/#24/#25), image uploads + MCP TLS (D014, PR #26) and
  the fixes since (PRs #28–#40) are in. See `docs/IMPLEMENTATION.md`.
- **Open follow-ups**: #19 (remaining by-path root operations in the
  install/update scripts), #38 (BoardPower refactor: one board-level
  power operation with a structured result, and a lab-wide per-outlet
  lock), #2 (in progress).

## Last Session

- **Date**: 2026-10-29
- **Branch**: `main`
- **Completed** (PRs #28–#37 deployed; #39, #40 not yet):
  - #28: tests never load the machine's real config or database;
    schema v11 repairs a pre-release `images` table
  - #29: `labctl mcp --http` waits for a busy port; `update.sh` waits
    through systemd auto-restarts
  - #30, #31: Kasa KLAP v2 (python-kasa ≥ 0.12 on 3.11+); KLAP-discovered
    strips controlled per outlet again
  - #32: `list_images` tells stdio sessions how to get an image in
  - #34: MCP `serial_send` `keys` / `data_hex` for control characters
  - #35: relay open failures reported cleanly, with a group hint
  - #37: `pre_power` bindings applied on every power-on path (#36),
    including boards sharing an outlet; relays checked before switching
  - #39: docs, README, CLI help and MCP tool descriptions audited
    against the code
  - #40: the SD card never leaves a board that may be running (`sdwire
    flash`/`update`, `boot-test` deploy; closes #23); strict
    `allow_admin_actuator_ops`; stored cycle delay range-checked; udev
    rules `root:dialout 0660`
- **Pending**: deploy #39/#40 (then `labctl serial udev --install
  --reload`); P2 (PyPI release); follow-ups #19, #38

## Earlier Session (Activity Stream)

- **Date**: 2026-04-20
- **Branch**: current working tree
- **Completed so far**:
  - Activity stream Phase A: `audit.emit()`, contextvars, schema v5, CLI query command
  - Activity stream Phase B: SSE broadcaster, `/activity` page, `/activity/stream`, `/api/activity`, CLI `--follow`
  - Activity stream Phase C: MCP mutating tools now run under audit context (`source=mcp`)
  - Activity stream Phase C: Flask request lifecycle now sets audit actor/source for API and web mutations
  - Regression tests added for MCP, API-key, and logged-in web attribution
  - Activity stream Phase D: `lab://activity/recent` and `lab://activity/{sbc_name}` MCP resources
  - Activity stream Phase D: `labctl activity export --format ndjson`
  - Activity stream Phase D: 30-day activity retention sweep wired into the existing claim-sweep loop
  - Config-path fixes for shared `/etc/labctl/config.yaml`, including `~` expansion and unreadable-path handling
  - Installer/update scripts now preserve existing config contents and repair shared-config permissions
- **Pending**:
  - Activity stream follow-up polish: newest-first ordering cleanup
  - Activity stream follow-up polish: timezone-aware timestamp rendering

## Previous Session

- **Date**: 2026-03-28
- **Completed**:
  - MCP (Model Context Protocol) server for AI assistant integration
  - Two-tier serial device management (serial_devices table, CLI commands, udev generation)
  - Kasa Smart Power Strip support (auto-detect, multi-outlet, KLAP auth, retry logic)
  - Native HTTPS for web server (--cert/--key flags, web: config section)
  - SBC rename support (CLI, API, web UI)
  - CLI logging initialization (basicConfig, -v/-q flags)
  - Fixed health check power probe, monitor ping under systemd, Kasa session cleanup
  - Default log level changed to WARNING, Kasa logging lowered to DEBUG
  - Kasa debug script (scripts/kasa-debug.py)
  - Sudoers/permissions setup for udev and ser2net without sudo

## Blockers

- On Python 3.10, Kasa devices on KLAP login version 2 (e.g. TP-Link HS300
  HW v2.0 firmware 1.1.2) can't be controlled: python-kasa 0.12, the first
  release that handles them, needs Python 3.11+, and 3.10 gets 0.7.7.
  Python 3.11+ installs are fixed (the failures, intermittent at first and
  total from Sep 2026, were python-kasa using KLAP v1 for these devices).
  Upstream: python-kasa #1604, #1648, PR #1731.

## Notes

- **All Milestones Complete!** (M1–M7 plus the features below)
- 2038 tests collected (2026-10-29); CI runs Python 3.10–3.13
- Database schema v12 (v8: actuators/bindings, v9: claim owners,
  v10/v11: uploaded images, v12: status history index)
- Schema migration is automatic and preserves existing data
- Two config files may need to be kept in sync (user + labctl system user)
  - Recommendation: use /etc/labctl/config.yaml as single source of truth
- HTTPS uses Flask's built-in ssl_context (suitable for lab use)
- Monitor service needs AmbientCapabilities=CAP_NET_RAW for ping to work
- MCP server available via `labctl mcp` (stdio) or `labctl mcp --http <port> [--host <addr>]`

## Milestones Summary

| Milestone | Description | Status |
|-----------|-------------|--------|
| M1 | Foundation (udev, ser2net, CLI) | Complete |
| M2 | Data Layer (database, manager) | Complete |
| M3 | Power Control (Tasmota/Shelly/Kasa) | Complete |
| M4 | CLI Completion | Complete |
| M5 | Web Interface (Flask, REST API) | Complete |
| M6 | Multi-Client Serial | Complete |
| M7 | Monitoring and Health | Complete |
| - | Deferred Items | Complete |
| - | Authentication | Complete |
| - | MCP Server | Complete |
| - | SDWire Support | Complete |
| - | Hardware Claims (PR #1; enforcement PRs #22/#24/#25) | Complete |
| - | Activity Stream | Complete |
| - | Actuators / Bindings (PR #4; all power paths PR #37) | Complete |
| P0 | PyPI release: installable + mcp 2.x SDK (PR #12) | Complete |
| P1 | PyPI release: safety (PRs #13–#18, #22–#26) | Complete, deployed |
| P2 | PyPI release: publish | Next |
