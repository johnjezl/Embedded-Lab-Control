# Embedded Lab Control (labctl)

## Project Overview
Centralized management system for embedded development lab resources. Provides deterministic USB serial access, power control, resource tracking, health monitoring, and AI integration for multiple SBCs.

## Key Documentation
- `docs/IMPLEMENTATION.md` - Milestone TODOs (update status here!)
- `docs/AGENT_RULES.md` - **READ THIS FIRST** - Development process rules
- `docs/DECISIONS.md` - Design decision log
- `docs/MCP_SERVER.md` - MCP server architecture and usage
- `STATUS.md` - Current project state
- `CHANGELOG.md` - Track all changes

## Development Rules (Summary)
1. Read `docs/AGENT_RULES.md` before starting any work
2. Follow the TODO list in `docs/IMPLEMENTATION.md`
3. Update TODO icons: ☐ → ✅ when complete, ⏸️ if deferred
4. Write tests for all code
5. **NEVER commit without user approval** - report changes and wait
6. Update `CHANGELOG.md` after each sub-milestone
7. Update `STATUS.md` at session start/end

## Current State
All milestones (M1-M7) complete; the PyPI release (P2 publish) is done, with the SDWire sudoers follow-up deferred to #47: 0.2.0 is published as `embedded-lab-control` (releases: `docs/RELEASING.md`). Additional features: authentication (session-based web + API key), MCP server (stdio or HTTP with API-key auth and TLS, two-step confirmation for destructive tools, host path allowlist, image uploads), hardware claims enforced on every surface, activity stream, serial device management, Kasa power strip support, SDWire, USB relay actuators with per-board bindings (`pre_power` straps applied on every power-on path), HTTPS.

## Tech Stack
- Python 3.10+
- Click (CLI)
- Flask (web/API)
- SQLite (database, schema v13)
- ser2net (serial over TCP)
- MCP SDK (AI integration via Model Context Protocol)
- python-kasa (TP-Link Kasa smart plug/strip control)
- sdwire (SD card multiplexer control — SDWire/SDWireC/SDWire3)
- pyserial (USB relay actuators — LCUS-1 driver)
- pytest (testing)

## Commands Reference
```bash
# Run tests
pytest tests/ -v

# Run specific test file
pytest tests/unit/test_mcp_server.py -v

# Format code
black src/ tests/
isort src/ tests/

# Lint
flake8 src/ tests/

# Install with all extras
pip install -e ".[dev,web,mcp,kasa,sdwire,actuators]"
```

## Key Architecture
- `src/labctl/core/` - Config, database, models, resource manager
- `src/labctl/cli.py` - Click CLI (all commands)
- `src/labctl/web/` - Flask web UI and REST API
- `src/labctl/power/` - Power controllers (Tasmota, Kasa, Shelly)
- `src/labctl/health/` - Health checks, monitoring daemon, alerts
- `src/labctl/serial/` - ser2net config, serial proxy, udev rules
- `src/labctl/sdwire/` - SDWire SD card multiplexer control
- `src/labctl/actuators/` - USB relay drivers (LCUS-1, mock) and binding runtime (pre_power straps, recovery composites)
- `src/labctl/services.py` - systemd service checks (`labctl services`)
- `src/labctl/mcp_server.py` - MCP server (resources, tools, prompts)
