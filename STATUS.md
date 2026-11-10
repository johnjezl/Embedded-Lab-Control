# Project Status

## Current State

- **Milestone**: Public PyPI release (`embedded-lab-control`)
- **Sub-task**: #73 session claims (after 0.3.0, published 2026-11-02).
  Phase 0, Phase 1 (safety) and P2 are done
- **Status**: `main` has everything merged through #69 (#47), CI green.
  0.3.0 covers #47, #50, #52, #56, #57, #60, #63, #65 (schema v14).
  Next: merge the release-prep PR, tag `v0.3.0`, and approve the `pypi`
  deployment (user). See `docs/IMPLEMENTATION.md`.
- **Open follow-ups**: #19 (remaining by-path root operations in the
  install/update scripts), #38 (BoardPower refactor: one board-level
  power operation with a structured result, and a lab-wide per-outlet
  lock); #48 coverage; #9 serial flow control.

## Last Session

- **Date**: 2026-11-08 to 2026-11-09
- **Branch**: `worktree-bridge-cse_014Q9aryST1Z35mUkKKvydup`, PR #74 (open,
  rebased onto #75, three review rounds, CI green)
- **Completed**:
  - #73: claims held for an MCP session (`claim_sbc(hold="session")`,
    schema v15 `claims.hold`, `claims.session_timeout_minutes`);
    `claim_status` / `claim_warnings` as a second content block in MCP
    tool results;
    `require_claim` refusals name the caller's lapsed claim; CLI, web,
    docs, `/deploy-and-test` skill updated (D015)
  - #75 (merged): the `kasa` extra installs `tzdata` on Python 3.10
    (GitHub's runners dropped the legacy `PST8PDT` zone and main's 3.10
    CI had gone red); a missing zone fails at once naming the install
    command. Follow-up #76 (remember a missing zone)
- **Next**: merge #74; deploy (schema v15), then reconnect stdio MCP
  agents and run `labctl skill install --force` in agent projects

## Previous Session (0.3.0)

- **Date**: 2026-11-02
- **Branch**: `release/0.3.0`
- **Completed**:
  - 0.3.0 prep: `__version__` 0.3.0; CHANGELOG `[Unreleased]` became
    `[0.3.0] - 2026-11-02` with a summary and upgrade notes (schema v14,
    reconnect stdio MCP agents, `Z` timestamps in REST), duplicate
    Added/Fixed headings merged, link definitions updated
- **Next**: merge, tag `v0.3.0` on main, check TestPyPI, approve the
  `pypi` deployment (user)

## Previous Session (0.2.x)

- **Date**: 2026-10-31
- **Branch**: `main` (PR work in worktrees)
- **Completed**:
  - #42, #43: #41 safety and config fixes (deployed 2026-10-30)
  - #44: long calls keep the caller's claim alive (#2; deployed
    2026-10-30)
  - #45: P2 release prep (lab-specific files removed, generic adapter
    guide, sdist with tests/config/docs, one-line missing-extra errors,
    PyPI install docs, CI triggers)
  - #46: release workflow (trusted publishing), version 0.2.0, CHANGELOG
    release section, `docs/RELEASING.md`
  - Issues #47-#52 filed from the Phase 0 scratch list
  - 0.2.0 tagged and published to PyPI, TestPyPI and GitHub Releases;
    clean install from PyPI checked
  - #53: README Features list covers SDWire, relays, boot testing, MCP
  - 0.2.1 prep: version and CHANGELOG section
  - #51: `labctl services status` asks systemctl for UTC times
  - #49: tests refuse non-loopback network access; fake clock and no
    settle sleeps (full run ~15 → ~11 min)
- **Next**: tag `v0.2.1` on main and approve the `pypi` deployment (user)

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
- Database schema v15 (v8: actuators/bindings, v9: claim owners,
  v10/v11: uploaded images, v12: status history index, v13: activity
  agent name, v14: claim and activity times converted to UTC, v15: claim
  hold for session claims)
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
