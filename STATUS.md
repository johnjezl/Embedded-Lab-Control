# Project Status

## Current State

- **Milestone**: Public PyPI release (`embedded-lab-control`)
- **Sub-task**: Phase 0 — installable + mcp 2.x SDK
- **Status**: Phase 0 complete on `release/phase0` (A SDK + annotations,
  B packaging, C0 lint debt, C1 CI). Not yet pushed; first GitHub Actions
  run happens on push. Next: Phase 1 (safety).

## Last Session

- **Date**: 2026-09-29
- **Branch**: `release/phase0` (from `main` @ `11ad19f`)
- **Completed**:
  - Issue triage: #3, #7 → P1 safety; #2, #9, #11 → post-release
  - MCP server on mcp 2.2 (`MCPServer`), tool calls serialized (D011)
  - `ToolAnnotations` on all 52 tools from a single table + enforcement test
  - `labctl mcp --http PORT --host ADDR` now honored (was 127.0.0.1:8000)
  - Packaged as `embedded-lab-control` 0.2.0.dev0; build/twine/pipx verified
  - Python 3.10 fix in `labctl.sdwire`; sdwire extra gated on ≥ 3.12
  - 996 tests passing on 3.10 and 3.12 (baseline on mcp 2.2: 791 passed,
    13 failed, 157 errors); coverage 68%
  - Repo clean under pinned black/isort/flake8 (`120990e`)
  - `.github/workflows/ci.yml`: 3.10–3.13 matrix (lint + pytest/coverage
    artifact) and full-history gitleaks job; 4 reviewed false positives in
    `.gitleaksignore`
- **Pending**:
  - Push `release/phase0` and confirm the first CI run (3.11/3.13 only
    exercised in CI so far)
  - Phase 1 safety: #3, #7, auth/confirm/dry-run (see
    `docs/development/RELEASE_NOTES_PHASE0.md`)
- **Environment notes**:
  - This host's `/tmp` is on the failing `/dev/sda` (#11); SQLite commits
    take ~1.2 s there. Run tests with `--basetemp` on `/dev/shm`
    (~2 min vs ~32 min).
  - Production `labctl-mcp` is still mcp 1.26 on `127.0.0.1:8000` via the
    `PYTHONPATH` overlay; see deployment notes in
    `docs/development/RELEASE_NOTES_PHASE0.md` before deploying.

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

- TP-Link HS300 HW v2.0 firmware 1.1.2+ has intermittent KLAP authentication failures
  - Workaround: retry logic (up to 2 retries) handles most cases
  - Workaround: enable "Third Party Compatibility" in Tapo app
  - Upstream python-kasa issues: #1604, #1603

## Notes

- **All Milestones Complete!**
- 996 tests passing (Python 3.10 and 3.12, 2026-09-29)
- Database schema v3: serial_devices, sdwire_devices/sdwire_assignments tables
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
