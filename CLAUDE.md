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
All milestones (M1-M7) complete. Additional features: authentication (session-based web + API key), MCP server, serial device management, Kasa power strip support, HTTPS.

## Tech Stack
- Python 3.10+
- Click (CLI)
- Flask (web/API)
- SQLite (database, schema v2)
- ser2net (serial over TCP)
- MCP SDK (AI integration via Model Context Protocol)
- python-kasa (TP-Link Kasa smart plug/strip control)
- sdwire (SD card multiplexer control — SDWire/SDWireC/SDWire3)
- pytest (testing, 322 tests)

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
pip install ".[web,mcp,kasa,sdwire]"
```

## Key Architecture
- `src/labctl/core/` - Config, database, models, resource manager
- `src/labctl/cli.py` - Click CLI (all commands)
- `src/labctl/web/` - Flask web UI and REST API
- `src/labctl/power/` - Power controllers (Tasmota, Kasa, Shelly)
- `src/labctl/health/` - Health checks, monitoring daemon, alerts
- `src/labctl/serial/` - ser2net config, serial proxy, udev rules
- `src/labctl/sdwire/` - SDWire SD card multiplexer control
- `src/labctl/mcp_server.py` - MCP server (resources, tools, prompts)
