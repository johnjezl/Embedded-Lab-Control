# Decision Log

This document records significant design decisions made during development.

---

## D001: Use physical USB path for udev rules

- **Date**: 2024-12-31
- **Context**: Need deterministic device naming for USB-TTL adapters
- **Options Considered**:
  1. USB device serial number
  2. Physical USB port path (KERNELS)
- **Decision**: Physical USB port path
- **Rationale**: 
  - USB hubs in use (Genesys Logic GL3523) lack unique serial numbers
  - Physical port mapping is intuitive for lab organization
  - Prevents issues when replacing identical adapters
  - Path format (e.g., `1-10.1.3`) directly maps to physical topology

---

## D002: Run on Ubuntu dev machine instead of dedicated SBC

- **Date**: 2024-12-31
- **Context**: Originally planned to use Pine64 A64+ as dedicated lab controller
- **Options Considered**:
  1. Dedicated Pine64 SBC
  2. Ubuntu development machine
- **Decision**: Ubuntu development machine
- **Rationale**:
  - Pine64 exhibited stability issues (kernel panics under load)
  - Dev machine is always on during work
  - More stable, proven hardware
  - Easier to maintain and update
  - One less device to manage
  - Can revisit dedicated hardware later if needed

---

## D003: Multi-client serial access approach

- **Date**: 2024-12-31
- **Context**: Need multiple users/agents to monitor same serial stream
- **Options Considered**:
  1. ser2net with `kickolduser: false`
  2. Custom proxy daemon with fan-out architecture
- **Decision**: Start with ser2net, add custom proxy in M6 if needed
- **Rationale**:
  - ser2net is simple and built-in
  - Handles basic multi-reader case
  - Custom proxy adds complexity
  - Defer until we understand actual usage patterns

---

## D004: Authentication approach for web UI and API

- **Date**: 2026-03-06
- **Context**: All 29 web endpoints were completely unprotected. Anyone with network access could control power, modify SBCs, and access serial consoles. Need authentication without adding new pip dependencies.
- **Options Considered**:
  1. Flask-Login with database-backed users
  2. Session-based web auth + API key auth using werkzeug.security (already bundled with Flask)
  3. OAuth2 / external identity provider
- **Decision**: Session-based web auth + API key auth using werkzeug.security and stdlib secrets/hmac
- **Rationale**:
  - No new dependencies — werkzeug.security ships with Flask, secrets/hmac are stdlib
  - Auth disabled by default — existing deployments and all existing tests continue working unchanged
  - Secure by default when enabled — `before_request` hook protects all routes; new routes are automatically protected
  - Web UI uses session cookies with CSRF tokens on all state-changing forms
  - API uses `X-API-Key` header with constant-time comparison (`hmac.compare_digest`)
  - `/api/health` remains open for monitoring tools
  - Users defined in config YAML — simple, no database migration needed
  - CLI provides user management commands for generating hashes and keys

---

## D005: Native HTTPS via Flask ssl_context

- **Date**: 2026-03-28
- **Context**: Web UI transmits credentials (when auth is enabled) over plain HTTP. Need HTTPS without requiring additional infrastructure.
- **Options Considered**:
  1. Reverse proxy (nginx/Caddy) in front of Flask
  2. Native SSL via Flask's `ssl_context` parameter
  3. Both (native + documented reverse proxy)
- **Decision**: Native SSL via Flask's `ssl_context` with self-signed certificates
- **Rationale**:
  - Zero additional dependencies or services — Flask/Werkzeug supports `ssl_context` natively
  - Simple for lab environments: generate a self-signed cert, add paths to config
  - CLI flags (`--cert`/`--key`) for ad-hoc use, config section (`web:`) for systemd
  - Reverse proxy remains recommended for internet-facing deployments but is not required

---

## D006: Kasa Smart Power Strip support via auto-detection

- **Date**: 2026-03-28
- **Context**: TP-Link Kasa HS300 power strip added to lab. Existing `KasaController` used `SmartPlug` class and ignored `plug_index`, so multi-outlet strips were not supported.
- **Options Considered**:
  1. Separate `KasaStripController` subclass
  2. Update `KasaController` to auto-detect device type via `Discover.discover_single()`
- **Decision**: Auto-detection in single `KasaController` class
- **Rationale**:
  - `Discover.discover_single()` returns the correct device type automatically
  - `device.children` provides outlet access for strips; single plugs have no children
  - No need for users to specify device type — `plug_index` is sufficient
  - TP-Link cloud credentials added to config (`kasa:` section) for KLAP-authenticated devices
  - Note: HS300 HW v2.0 firmware 1.1.2+ has known KLAP auth issues; workaround is Tapo app "Third Party Compatibility" toggle

---

## D007: Two-tier serial device management

- **Date**: 2026-03-28
- **Context**: USB-serial adapters were managed via manual YAML file editing (port-mapping.yaml), requiring udev rule regeneration scripts. Adding/removing adapters as projects change was excessive overhead.
- **Options Considered**:
  1. Keep YAML-based workflow, add CLI to edit the YAML
  2. Move device registry to the database with CLI management and separate assignment aliases
- **Decision**: Database-backed two-tier model — serial devices (physical adapters) and port assignments (SBC connections with aliases)
- **Rationale**:
  - Devices registered once with generic names (e.g., `port-1`), reusable across SBCs
  - Assignments get meaningful aliases (e.g., `jetson-console`) that travel with the SBC, not the adapter
  - Udev rules auto-generated from database via `labctl serial udev --install`
  - Pure-Python USB discovery via `/sys` and `udevadm` — no external script dependency
  - Schema v2 migration is additive (ALTER TABLE ADD COLUMN) — preserves existing data
  - `labctl connect` resolves by alias → SBC name → filesystem path for flexibility

---

## D008: Systemd service permissions for ping and udev

- **Date**: 2026-03-28
- **Context**: Monitor daemon's health checks failed to ping SBCs due to systemd's `NoNewPrivileges=yes` blocking ping's `cap_net_raw` capability. Udev rule installation and ser2net restart required sudo.
- **Options Considered**:
  1. Remove all systemd security hardening
  2. Targeted capability grants and group permissions
- **Decision**: Targeted permissions — `AmbientCapabilities=CAP_NET_RAW` for monitor, group-writable udev rules file, sudoers entries for udevadm and ser2net restart
- **Rationale**:
  - Minimal privilege escalation — only CAP_NET_RAW for ping, not full root
  - Group-writable rules file avoids sudo for file writes
  - Passwordless sudoers entries scoped to specific commands only
  - Other systemd hardening (ProtectSystem, ProtectHome, PrivateTmp) preserved

---

## D008b: Sudo for SD card mount/flash operations

- **Date**: 2026-04-02
- **Context**: SDWire file operations (`mount`, `umount`, `dd`, `sync`,
  `partprobe`) require root. The MCP server and CLI run as unprivileged users
  (`labctl` service, `john` interactive).
- **Options Considered**:
  1. Run MCP server as root
  2. Sudoers rules for specific commands
  3. udisks2 for mounting
- **Decision**: Passwordless sudoers rules in `/etc/sudoers.d/labctl` for
  `mount`, `umount`, `dd`, `sync`, and `partprobe`. The controller prefixes
  those privileged commands with `sudo`. Mount uses `-o uid=<user>,gid=<group>`
  so the calling user can write to FAT partitions.
- **Rationale**:
  - Minimal privilege — only the specific SDWire commands, not full root
  - Consistent with D008's approach for udevadm/ser2net
  - udisks2 would add a dependency and doesn't cover dd/sync/partprobe
  - FAT uid/gid mount option avoids needing sudo for the file copy itself
  - The implementation uses the Python `sdwire` library directly, not
    `sd-mux-ctrl`, so no extra SD mux binary needs sudo access

---

## D009: MCP server for AI assistant integration

- **Date**: 2026-03-28
- **Context**: AI assistants (Claude Desktop, Claude Code) can manage external systems via the Model Context Protocol. Exposing lab resources through MCP enables AI-assisted lab management — debugging SBCs, checking health, controlling power — through natural language.
- **Options Considered**:
  1. Expose the existing REST API directly (requires AI to know HTTP endpoints)
  2. Build an MCP server as a thin wrapper around the existing ResourceManager
  3. Build a standalone MCP service with its own data layer
- **Decision**: MCP server as thin wrapper using the official `mcp` Python SDK's FastMCP API
- **Rationale**:
  - Zero duplicated business logic — MCP tools/resources call the same manager methods as the CLI and web API
  - FastMCP generates JSON Schema from type hints and docstrings automatically
  - stdio transport for local use (standard for Claude Desktop/Code), HTTP available for remote
  - Resources for read-only data (SBC list, power state, health), tools for mutations (power control, SBC management)
  - Prompts provide guided workflows (debug-sbc, lab-report) for common tasks
  - `mcp` is an optional dependency — doesn't affect existing installations
  - See `docs/MCP_SERVER.md` for full architecture documentation

---

## D010: SDWire SD card multiplexer integration

- **Date**: 2026-03-31
- **Context**: Lab uses SDWire and SDWireC devices to switch SD cards between SBCs and the dev machine for automated flashing, eliminating manual SD card swapping.
- **Options Considered**:
  1. Shell out to `sd-mux-ctrl` C++ tool
  2. Use the `sdwire` Python library directly
- **Decision**: Use the `sdwire` Python library as an optional dependency
- **Rationale**:
  - Pure Python — no need to build C++ tools from source
  - Supports all three device types (SDWire, SDWireC, SDWire3) with a unified API
  - Provides block device mapping for automated image flashing
  - Same pattern as other optional hardware (Kasa, MCP) — graceful ImportError handling
  - `labctl sdwire flash` command automates the full workflow: switch to host, write image, switch to DUT, power cycle

---

_Template for new decisions:_

```markdown
## DXXX: <Title>

- **Date**: YYYY-MM-DD
- **Context**: <Why this decision was needed>
- **Options Considered**:
  1. <Option 1>
  2. <Option 2>
- **Decision**: <What was chosen>
- **Rationale**: <Why this option was selected>
```
