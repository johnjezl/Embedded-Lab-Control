# Project Status

## Current State

- **Milestone**: Activity Stream
- **Sub-task**: Phase C — source fidelity complete
- **Status**: Phase A, Phase B, and Phase C complete

## Last Session

- **Date**: 2026-04-20
- **Branch**: current working tree
- **Completed so far**:
  - Activity stream Phase A: `audit.emit()`, contextvars, schema v5, CLI query command
  - Activity stream Phase B: SSE broadcaster, `/activity` page, `/activity/stream`, `/api/activity`, CLI `--follow`
  - Activity stream Phase C: MCP mutating tools now run under audit context (`source=mcp`)
  - Activity stream Phase C: Flask request lifecycle now sets audit actor/source for API and web mutations
  - Regression tests added for MCP, API-key, and logged-in web attribution
  - Config-path fixes for shared `/etc/labctl/config.yaml`, including `~` expansion and unreadable-path handling
  - Installer/update scripts now preserve existing config contents and repair shared-config permissions
- **Pending**:
  - Rerun broader test slices in `.venv`
  - Update implementation notes for completed Phase C work once merged

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

- TP-Link HS300 HW v2.0 firmware 1.1.2+ has intermittent KLAP authentication failures
  - Workaround: retry logic (up to 2 retries) handles most cases
  - Workaround: enable "Third Party Compatibility" in Tapo app
  - Upstream python-kasa issues: #1604, #1603

## Notes

- **All Milestones Complete!**
- 322 tests passing
- Database schema v3: serial_devices, sdwire_devices/sdwire_assignments tables
- Schema migration is automatic and preserves existing data
- Two config files may need to be kept in sync (user + labctl system user)
  - Recommendation: use /etc/labctl/config.yaml as single source of truth
- HTTPS uses Flask's built-in ssl_context (suitable for lab use)
- Monitor service needs AmbientCapabilities=CAP_NET_RAW for ping to work
- MCP server available via `labctl mcp` (stdio) or `labctl mcp --http <port>`

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
