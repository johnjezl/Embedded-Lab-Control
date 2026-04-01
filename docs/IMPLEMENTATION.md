# Lab Controller System - Implementation Plan

## Overview

This document outlines the phased implementation of the Lab Controller System. Each milestone is designed to deliver incremental, usable functionality while building toward the complete system.

## Icon Key

| Icon | Meaning |
|------|---------|
| ☐ | Not started |
| ✅ | Complete |
| ⏸️ | Deferred to later phase |
| 🔗 | Has dependency on another milestone (shown as ⏸️🔗 or ☐🔗) |

## Milestone Summary

| Milestone | Focus | Deliverable |
|-----------|-------|-------------|
| M1 | Foundation | udev rules, ser2net, basic CLI |
| M2 | Data Layer | Database, resource management |
| M3 | Power Control | Smart plug integration |
| M4 | CLI Completion | Full CLI functionality |
| M5 | Web Interface | Dashboard and REST API |
| M6 | Multi-Client Serial | Shared serial access |
| M7 | Monitoring | Health checks and status tracking |
| Auth | Authentication | Web login, API keys, CSRF |

---

## Milestone 1: Foundation

**Goal**: Establish deterministic serial access with basic CLI for listing and connecting.

### Prerequisites
- ✅ Ubuntu development machine with USB ports
- ✅ At least one powered USB hub
- ✅ At least one USB-TTL adapter
- ✅ ser2net installed (`apt install ser2net`)

### TODO List

#### 1.1 Project Setup
- ✅ Create project directory structure
  ```
  mkdir -p labctl/{src/labctl,config/udev,docs,tests,scripts}
  ```
- ✅ Initialize Python project
  - ✅ Create `pyproject.toml` with dependencies (click, pyyaml)
  - ✅ Create virtual environment
  - ✅ Create `src/labctl/__init__.py`

#### 1.2 udev Rules System
- ✅ Create discovery script `scripts/discover-usb-serial.sh`
  - ✅ Enumerate all connected ttyUSB/ttyACM devices
  - ✅ Extract KERNELS path for each
  - ✅ Output in format suitable for rule generation (table and JSON)
- ✅ Create udev rule template generator `scripts/generate-udev-rules.py`
  - ✅ Input: mapping of physical port to logical name (YAML)
  - ✅ Output: `/etc/udev/rules.d/99-lab-serial.rules`
- ✅ Document physical hub layout
  - ✅ Create `docs/HARDWARE_MAP.md`
  - ✅ Diagram of hub topology
  - ✅ Port assignment table
- ✅ Create installation script `scripts/install-udev.sh`
  - ✅ Copy rules to `/etc/udev/rules.d/`
  - ✅ Reload udev rules
  - ✅ Trigger udev to create symlinks
- ✅ Verify symlinks created under `/dev/lab/`

#### 1.3 ser2net Configuration
- ✅ Create ser2net config generator `src/labctl/serial/ser2net.py`
  - ✅ Function to generate YAML config from port mapping
  - ✅ Support for baud rate configuration
  - ✅ Default options (local, no kickolduser)
- ✅ Create initial config `/etc/ser2net.yaml`
  - ✅ Configure 5 ports (TCP 4000-4004)
- ✅ Enable and start ser2net service
  ```
  sudo systemctl enable ser2net
  sudo systemctl start ser2net
  ```
- ✅ Verify TCP access works
  - ✅ Test with `nc localhost <port>`
  - ✅ Test with `labctl console` (replaces picocom/minicom)

#### 1.4 Basic CLI Structure
- ✅ Create CLI entry point `src/labctl/cli.py`
  - ✅ Use Click framework
  - ✅ Implement command groups
- ✅ Implement `labctl ports` command
  - ✅ List `/dev/lab/*` symlinks
  - ✅ Show device path, symlink name
  - ✅ Show if ser2net port is configured
- ✅ Implement `labctl connect <port-name>` command
  - ✅ Open TCP connection to ser2net port
  - ✅ Spawn `nc` or `telnet` for TCP, `picocom` for direct
- ✅ Create console entry point in `pyproject.toml`
  ```toml
  [project.scripts]
  labctl = "labctl.cli:main"
  ```
- ✅ Test CLI installation with `pip install -e .`

#### 1.5 Configuration File
- ✅ Create config loader `src/labctl/core/config.py`
  - ✅ Load YAML config from `~/.config/labctl/config.yaml`
  - ✅ Support environment variable overrides (LABCTL_DEV_DIR, etc.)
  - ✅ Provide sensible defaults
- ✅ Create default config template `config/labctl.yaml.example`
- ✅ Add config initialization to CLI (`-c/--config` option)

### Acceptance Criteria
- ✅ USB-TTL adapters appear as `/dev/lab/<name>` symlinks
- ✅ Unplugging and replugging maintains same symlink name
- ✅ `labctl ports` lists all configured serial ports
- ✅ `labctl connect <port>` opens serial console via TCP
- ✅ ser2net survives service restart

---

## Milestone 2: Data Layer

**Goal**: Implement persistent storage for SBC resources and CLI management commands.

### TODO List

#### 2.1 Database Setup
- ✅ Create database module `src/labctl/core/database.py`
  - ✅ SQLite connection management
  - ✅ Schema initialization
  - ✅ Migration support (simple version table)
- ✅ Implement schema from ARCHITECTURE.md
  - ✅ `sbcs` table
  - ✅ `serial_ports` table
  - ✅ `network_addresses` table
  - ✅ `power_plugs` table
  - ✅ `status_log` table
  - ✅ `audit_log` table
- ✅ Create database initialization on first run
- ✅ Add database path to config

#### 2.2 Data Models
- ✅ Create models module `src/labctl/core/models.py`
  - ✅ `SBC` dataclass
  - ✅ `SerialPort` dataclass
  - ✅ `NetworkAddress` dataclass
  - ✅ `PowerPlug` dataclass
  - ✅ `Status` enum
- ✅ Implement model serialization (to/from database)

#### 2.3 Resource Manager
- ✅ Create manager module `src/labctl/core/manager.py`
  - ✅ `ResourceManager` class
  - ✅ CRUD operations for SBCs
  - ✅ Port assignment operations
  - ✅ Network address operations
  - ✅ Query/filter operations

#### 2.4 CLI SBC Management Commands
- ✅ Implement `labctl list`
  - ✅ Tabular output of all SBCs
  - ✅ Columns: name, project, status, console port, IP
  - ✅ Filter by `--project` and `--status`
- ✅ Implement `labctl add <name>`
  - ✅ Options: `--project`, `--description`
  - ✅ Validate unique name
- ✅ Implement `labctl remove <name>`
  - ✅ Confirm before deletion
  - ✅ Cascade delete related records
- ✅ Implement `labctl info <name>`
  - ✅ Detailed view of single SBC
  - ✅ All ports, addresses, power plug
- ✅ Implement `labctl edit <name>`
  - ✅ Update project, description

#### 2.5 CLI Port Assignment Commands
- ✅ Implement `labctl port assign <sbc> <type> <device>`
  - ✅ Types: console, jtag, debug
  - ✅ Auto-assign TCP port from pool
  - ✅ `--tcp-port` override
  - ✅ `--baud` option
- ✅ Implement `labctl port remove <sbc> <type>`
- ✅ Implement `labctl port list`
  - ✅ Show all port assignments
  - ✅ Show unassigned `/dev/lab/*` devices (`--unassigned` flag)

#### 2.6 CLI Network Commands
- ✅ Implement `labctl network set <sbc> <type> <ip>`
  - ✅ Types: ethernet, wifi
  - ✅ Options: `--mac`, `--hostname`
- ✅ Implement `labctl network remove <sbc> <type>`

#### 2.7 ser2net Integration
- ✅ Update ser2net config generator
  - ✅ Read assignments from database
  - ✅ Generate complete config
- ✅ Implement `labctl ser2net generate`
  - ✅ Output to stdout or file
  - ✅ `--install` flag to copy to `/etc/ser2net.yaml`
- ✅ Implement `labctl ser2net reload`
  - ✅ Restart ser2net service

### Acceptance Criteria
- ✅ SBC records persist across restarts
- ✅ `labctl add/remove/list/info` work correctly
- ✅ Port assignments update ser2net config
- ✅ `labctl console <sbc>` works with named SBCs (implemented in M4)

---

## Milestone 3: Power Control

**Goal**: Integrate smart plug control for remote power management.

### TODO List

#### 3.1 Power Controller Framework
- ✅ Create power module `src/labctl/power/`
- ✅ Define abstract base `src/labctl/power/base.py`
  - ✅ `PowerController` ABC
  - ✅ Methods: `power_on`, `power_off`, `power_cycle`, `get_state`
  - ✅ `PowerState` enum: ON, OFF, UNKNOWN
- ✅ Create controller factory
  - ✅ Return correct implementation based on plug type

#### 3.2 Tasmota Implementation
- ✅ Create `src/labctl/power/tasmota.py`
  - ✅ HTTP API client
  - ✅ Implement all `PowerController` methods
  - ✅ Handle multi-relay devices (index parameter)
  - ✅ Error handling and timeouts
- ⏸️ Test with actual Tasmota device (pending hardware)

#### 3.3 Kasa Implementation (Optional)
- ✅ Add `python-kasa` dependency (optional)
- ✅ Create `src/labctl/power/kasa.py`
  - ✅ Async wrapper for python-kasa
  - ✅ Implement all `PowerController` methods
- ✅ Test with Kasa device if available

#### 3.4 Shelly Implementation (Optional)
- ✅ Create `src/labctl/power/shelly.py`
  - ✅ HTTP API client
  - ✅ Implement all `PowerController` methods
- ⏸️ Test with Shelly device if available

#### 3.5 CLI Plug Assignment
- ✅ Implement `labctl plug assign <sbc> <type> <address>`
  - ✅ Types: tasmota, kasa, shelly
  - ✅ `--index` for multi-outlet strips
- ✅ Implement `labctl plug remove <sbc>`

#### 3.6 CLI Power Commands
- ✅ Implement `labctl power <sbc> on`
- ✅ Implement `labctl power <sbc> off`
- ✅ Implement `labctl power <sbc> cycle`
  - ✅ `--delay` option (default 2s)
- ✅ Implement `labctl power <sbc> status`
- ✅ Implement `labctl power-all on|off`
  - ✅ `--project` filter
  - ✅ Confirmation prompt

### Acceptance Criteria
- ✅ Can assign smart plugs to SBCs in database
- ✅ `labctl power <sbc> on/off/cycle` controls actual hardware
- ✅ `labctl power <sbc> status` shows current power state
- ✅ Power operations logged in audit log

---

## Milestone 4: CLI Completion

**Goal**: Polish CLI with all planned commands and improved UX.

### TODO List

#### 4.1 Console Commands Enhancement
- ✅ Improve `labctl console <sbc>`
  - ✅ Auto-detect port type (prefer console)
  - ✅ `--type` option for jtag/debug
  - ✅ Better error messages
- ✅ Implement `labctl log <sbc>`
  - ✅ Connect and log to file
  - ✅ `--follow` for continuous output
  - ✅ `--lines` to capture N lines then exit
  - ✅ Timestamped output

#### 4.2 Status Commands
- ✅ Implement `labctl status`
  - ✅ Overview of all SBCs
  - ✅ Color-coded status (green/red/yellow)
  - ✅ `--watch` for continuous update
- ✅ Implement `labctl health-check` (implemented in M7)
  - ✅ Ping all SBCs
  - ✅ Check serial port availability
  - ✅ Check power plug connectivity
  - ✅ Summary report

#### 4.3 SSH Integration
- ✅ Implement `labctl ssh <sbc>`
  - ✅ Look up IP from database
  - ✅ Spawn SSH with configured user
  - ✅ `--user` override
- ✅ Store default SSH user in SBC record

#### 4.4 Import/Export
- ✅ Implement `labctl export`
  - ✅ Export all SBCs to YAML/JSON
  - ✅ `--format` option
- ✅ Implement `labctl import <file>`
  - ✅ Import SBC definitions
  - ✅ Handle conflicts (skip/update/error)

#### 4.5 CLI Polish
- ✅ Add `--verbose` global flag (already exists)
- ✅ Add `--quiet` global flag
- ✅ Improve help text for all commands
- ✅ Add command aliases (ls=list, rm=remove, show=info, on/off=power)
- ✅ Add shell completion support
  - ✅ Bash completion
  - ✅ Zsh completion
  - ✅ Fish completion

#### 4.6 Error Handling
- ✅ Consistent error message format
- ✅ Meaningful exit codes
- ✅ Graceful handling of:
  - ✅ Missing database
  - ✅ Network timeouts
  - ✅ Invalid configurations

### Acceptance Criteria
- ✅ All CLI commands from spec implemented
- ✅ Consistent UX across all commands
- ✅ Helpful error messages
- ✅ Shell completion works

---

## Milestone 5: Web Interface

**Goal**: Provide web-based dashboard and REST API.

### TODO List

#### 5.1 Flask Application Setup
- ✅ Create web module `src/labctl/web/`
- ✅ Create Flask app `src/labctl/web/app.py`
  - ✅ Application factory pattern
  - ✅ Configuration loading
  - ✅ Database connection
- ✅ Add Flask dependencies (flask, flask-socketio)
- ✅ Create CLI command `labctl web`
  - ✅ Start web server
  - ✅ `--host` and `--port` options

#### 5.2 REST API - SBC Endpoints
- ✅ `GET /api/sbcs` - List all SBCs
- ✅ `GET /api/sbcs/<name>` - Get SBC details
- ✅ `POST /api/sbcs` - Create SBC
- ✅ `PUT /api/sbcs/<name>` - Update SBC
- ✅ `DELETE /api/sbcs/<name>` - Delete SBC
- ✅ Implement JSON serialization for models
- ✅ Error handling with proper HTTP status codes

#### 5.3 REST API - Power Endpoints
- ✅ `POST /api/sbcs/<name>/power` - Power control
  - ✅ Body: `{"action": "on|off|cycle"}`
- ✅ `GET /api/sbcs/<name>/power` - Power status

#### 5.4 REST API - Serial Endpoints
- ✅ `GET /api/sbcs/<name>/console/info` - Console connection info
- ✅ `GET /api/ports` - List available serial ports

#### 5.5 REST API - Status Endpoints
- ✅ `GET /api/health` - System health
- ✅ `GET /api/status` - All SBC statuses

#### 5.6 Web Dashboard - Templates
- ✅ Create base template with navigation
- ✅ Create dashboard page `templates/dashboard.html`
  - ✅ Grid of SBC cards
  - ✅ Status indicators
  - ✅ Quick action buttons
- ✅ Create SBC detail page `templates/sbc_detail.html`
  - ✅ All SBC information
  - ✅ Edit form
  - ✅ Port/network/plug assignment forms
- ✅ Create settings page `templates/settings.html`

#### 5.7 Web Dashboard - Styling
- ✅ Create CSS `static/css/style.css`
  - ✅ Clean, minimal design
  - ✅ Status colors (green/red/yellow)
  - ✅ Responsive layout
- ✅ Add JavaScript `static/js/app.js`
  - ✅ AJAX helpers
  - ⏸️ Status refresh (deferred)

#### 5.8 WebSocket - Real-time Updates
- ⏸️ Implement Flask-SocketIO integration (deferred to M6)
- ⏸️ Push status updates to connected clients
- ⏸️ Update dashboard without refresh

#### 5.9 Web Console (xterm.js)
- ✅ Add xterm.js to static assets (implemented in M6)
- ✅ Create console page `templates/console.html`
- ✅ Implement WebSocket bridge to ser2net
- ✅ Bidirectional data flow
- ⏸️ Multiple console tabs

### Acceptance Criteria
- ✅ REST API fully functional
- ✅ Dashboard shows all SBCs with status
- ✅ Can control power from web interface
- ✅ Web-based serial console works (implemented in M6)
- ⏸️ Real-time WebSocket status updates (would need Flask-SocketIO)

---

## Milestone 6: Multi-Client Serial Access

**Goal**: Enable multiple clients to simultaneously access serial streams.

### TODO List

#### 6.1 Requirements Analysis
- ✅ Document use cases
  - ✅ Multiple viewers (watch-only)
  - ✅ Single writer, multiple readers
  - ⏸️ Multiple writers (needs arbitration) - deferred, using first-writer-wins
- ✅ Decide on initial scope (likely: one writer, many readers)

#### 6.2 Serial Proxy Daemon
- ✅ Create proxy module `src/labctl/serial/proxy.py`
- ✅ Implement fan-out architecture
  - ✅ Single connection to ser2net (or direct serial)
  - ✅ Multiple client connections
  - ✅ Broadcast reads to all clients
- ✅ Write arbitration
  - ✅ Option A: First client gets write lock (implemented)
  - ⏸️ Option B: All clients can write (risk conflicts) - configurable
  - ⏸️ Option C: Queue-based writes - deferred
- ✅ Session management
  - ✅ Track connected clients
  - ✅ Graceful disconnect handling

#### 6.3 Proxy Configuration
- ✅ Add proxy settings to config
  - ✅ Proxy port range
  - ✅ Write policy
- ✅ Proxy port allocation scheme

#### 6.4 CLI Integration
- ⏸️ Update `labctl connect` to use proxy when multiple clients - deferred
- ✅ Add `labctl proxy start <sbc>` - start proxy for SBC
- ✅ Add `labctl proxy list` - list running proxies
- ✅ Add `labctl sessions <sbc>` - list connected clients

#### 6.5 Web Integration
- ✅ Create web console page with xterm.js
- ✅ WebSocket bridge for browser-to-proxy communication
- ⏸️ Show connected clients in UI - deferred

#### 6.6 Session Logging (Optional)
- ✅ Log all serial traffic to files
- ✅ Configurable log directory
- ✅ Log rotation with compression and cleanup

### Acceptance Criteria
- ✅ Multiple CLI clients can view same console
- ✅ Web and CLI can view same console simultaneously
- ✅ Write conflicts handled gracefully (first-writer-wins)
- ✅ No data loss on client disconnect

---

## Milestone 7: Monitoring and Health

**Goal**: Automated health checking and status tracking.

### TODO List

#### 7.1 Health Check Module
- ✅ Create health module `src/labctl/health/`
- ✅ Implement ping check
  - ✅ ICMP ping to SBC IPs
  - ✅ Configurable timeout
- ✅ Implement serial probe
  - ✅ Check if port opens successfully
  - ✅ Optional: Send probe string, check response
- ✅ Implement power check
  - ✅ Query plug status

#### 7.2 Health Check CLI
- ✅ Enhance `labctl health-check`
  - ✅ Run all check types
  - ✅ `--type ping|serial|power`
  - ✅ `--sbc <name>` for single SBC
  - ✅ Output: table with check results

#### 7.3 Status Tracking
- ✅ Update status on each check
- ✅ Store history in `status_log` table
- ✅ Retention policy (configurable days)

#### 7.4 Monitoring Daemon (Optional)
- ✅ Create daemon `src/labctl/health/daemon.py`
- ✅ Periodic health checks
- ✅ Configurable interval
- ✅ CLI command `labctl monitor`
  - ✅ `--foreground` for debug
  - ⏸️ `--daemon` for background (use systemd instead)
- ✅ Systemd service file

#### 7.5 Alerting (Future)
- ✅ Define alert conditions
  - ✅ SBC offline
  - ✅ Power state change
  - ✅ Serial disconnect
- ✅ Alert channels (stub for future)
  - ✅ Log file
  - ✅ Email (stub)
  - ✅ Slack webhook (stub)

#### 7.6 Status Dashboard Enhancements
- ✅ Historical status view
- ✅ Uptime tracking
- ✅ Status timeline

### Acceptance Criteria
- ✅ `labctl health-check` runs all checks
- ✅ Status history stored in database
- ✅ Dashboard shows current status
- ✅ Monitoring daemon runs in foreground

---

## Authentication

**Goal**: Protect web UI and API endpoints with optional authentication.

### TODO List

#### Auth Configuration
- ✅ Add `UserConfig` dataclass (username, password_hash, api_key)
- ✅ Add `AuthConfig` dataclass (enabled, users, secret_key, session_lifetime_minutes)
- ✅ Wire auth config into `Config.from_dict()` and `to_dict()`
- ✅ Auth disabled by default — existing deployments unaffected

#### Web Authentication
- ✅ Create auth module `src/labctl/web/auth.py`
  - ✅ User lookup by username and API key (constant-time comparison)
  - ✅ Password verification with `werkzeug.security`
  - ✅ Session-based CSRF token generation and validation
  - ✅ Login/logout routes via `auth_bp` Blueprint
- ✅ Create login page template `templates/login.html`
- ✅ Add logout button to navbar in `base.html`
- ✅ Add CSRF tokens to all POST forms (dashboard and sbc_detail)
- ✅ Wire auth into app factory (`app.py`)
  - ✅ `before_request` hook for session/API key enforcement
  - ✅ `before_request` hook for CSRF validation
  - ✅ Whitelist: login, logout, static files, `/api/health`
  - ✅ `csrf_token()` Jinja2 template global

#### API Authentication
- ✅ `X-API-Key` header authentication for all `/api/*` endpoints
- ✅ Constant-time key comparison with `hmac.compare_digest`
- ✅ `/api/health` remains open for monitoring tools

#### CLI User Management
- ✅ `labctl user hash-password` — generate password hash
- ✅ `labctl user generate-key` — generate random API key
- ✅ `labctl user add <username>` — interactive creation with YAML output
- ✅ `labctl user verify <username>` — verify password against config

#### Packaging Fix
- ✅ Add `[tool.setuptools.package-data]` to `pyproject.toml` for templates/static

#### Testing
- ✅ 14 auth integration tests covering login/logout, web redirect, API key, CSRF, health open, auth-disabled default
- ✅ All 188 tests passing (existing 174 + 14 new)

### Acceptance Criteria
- ✅ Auth disabled by default — all existing tests pass unchanged
- ✅ When enabled, web routes redirect to login
- ✅ When enabled, API routes require `X-API-Key` header
- ✅ `/api/health` always open
- ✅ CSRF tokens protect all state-changing web forms
- ✅ No new pip dependencies

---

## Appendix A: Dependencies

### Python Packages
```
click>=8.0
pyyaml>=6.0
flask>=3.0
flask-socketio>=5.0
python-kasa>=0.5 (optional)
requests>=2.28
```

### System Packages
```
ser2net
python3-venv
```

### Optional
```
picocom or minicom (for direct serial access)
```

## Appendix B: Testing Strategy

### Unit Tests (188 total)
- ✅ Database operations (test_database.py - 8 tests)
- ✅ Model serialization (test_manager.py - 19 tests)
- ✅ Config loading (test_config.py - 17 tests)
- ✅ Power controller mocked (test_power.py - 19 tests)
- ✅ Serial proxy (test_proxy.py - 26 tests)
- ✅ Health checks (test_health.py - 28 tests)
- ✅ ser2net config (test_ser2net.py - 10 tests)

### Integration Tests
- ✅ CLI commands (test_cli.py - 8 tests)
- ✅ REST API endpoints (test_web.py - 36 tests)
- ✅ Authentication (test_auth.py - 14 tests)

### Manual Tests
- ✅ Full workflow with real hardware
- ✅ Power control with actual plugs
- ✅ Serial console with real SBC

## Appendix C: Documentation

### User Documentation
- ✅ README.md - Installation, quick start, CLI reference, API reference
- ✅ Hardware setup guide (HARDWARE_MAP.md)

### Developer Documentation
- ✅ Architecture overview (IMPLEMENTATION.md)
- ✅ Development rules (AGENT_RULES.md)
- ✅ Decision log (DECISIONS.md)

## Appendix D: Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | TBD | Initial implementation plan |
