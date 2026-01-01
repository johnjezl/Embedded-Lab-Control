# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Project documentation structure
  - ARCHITECTURE.md - System design and component specifications
  - IMPLEMENTATION.md - Milestone-based TODO lists
  - AGENT_RULES.md - Development process and standards
  - STATUS.md - Current project state tracking
  - CHANGELOG.md - This file
- M1.1 Project Setup (2025-12-31)
  - Created project directory structure (src/labctl, tests, scripts, config, docs)
  - Added pyproject.toml with dependencies (click, pyyaml, requests)
  - Created virtual environment with dev tools (pytest, black, isort, flake8)
  - Installed ser2net system package
- M1.2 udev Rules System (2025-12-31)
  - Created `scripts/discover-usb-serial.sh` for USB device enumeration
  - Created `scripts/generate-udev-rules.py` for rule generation from YAML mapping
  - Created `scripts/install-udev.sh` for udev rules installation
  - Created `docs/HARDWARE_MAP.md` with USB topology documentation
  - Symlinks now created under `/dev/lab/` for deterministic device access
- M1.3 ser2net Configuration (2025-12-31)
  - Created `src/labctl/serial/ser2net.py` for config generation
  - Support for baud rate, parity, local-only connections
  - Generated config for 5 ports on TCP 4000-4004
  - All ports verified accessible via TCP
- M1.4 Basic CLI Structure (2025-12-31)
  - Created `src/labctl/cli.py` with Click framework
  - Implemented `labctl ports` - lists configured serial ports with TCP mappings
  - Implemented `labctl connect <port>` - connects via TCP or direct serial
  - Added 8 CLI integration tests
- M1.5 Configuration File (2025-12-31)
  - Created `src/labctl/core/config.py` with Config dataclasses
  - YAML config loading from ~/.config/labctl/config.yaml
  - Environment variable overrides (LABCTL_DEV_DIR, LABCTL_BASE_TCP_PORT, etc.)
  - Added `-c/--config` CLI option
  - Added 17 unit tests for config module
  - **Milestone 1 Complete!**
- M2.1-2.5 Data Layer (2025-12-31)
  - Created `src/labctl/core/database.py` with SQLite schema
    - Tables: sbcs, serial_ports, network_addresses, power_plugs, status_log, audit_log
    - Foreign keys with cascade delete
    - Schema versioning support
  - Created `src/labctl/core/models.py` with data models
    - SBC, SerialPort, NetworkAddress, PowerPlug dataclasses
    - Status, PortType, AddressType, PlugType enums
    - from_row() methods for database deserialization
  - Created `src/labctl/core/manager.py` with ResourceManager
    - Full CRUD operations for SBCs
    - Serial port, network address, power plug assignment
    - Audit logging for all operations
  - Added CLI SBC management commands
    - `labctl list` - tabular output with filters (--project, --status)
    - `labctl add <name>` - create SBC with options
    - `labctl remove <name>` - delete with confirmation
    - `labctl info <name>` - detailed SBC view
    - `labctl edit <name>` - update properties
  - Added CLI port assignment commands
    - `labctl port assign <sbc> <type> <device>` - with auto TCP port
    - `labctl port remove <sbc> <type>`
    - `labctl port list` - all port assignments
  - Added 27 new tests (8 database, 19 manager)
  - Total: 62 tests passing
- M2.6-2.7 Network and ser2net Commands (2025-12-31)
  - Added `labctl network set <sbc> <type> <ip>` with --mac, --hostname options
  - Added `labctl network remove <sbc> <type>`
  - Added `labctl ser2net generate` - generates config from database
    - Supports --output file and --install flag
  - Added `labctl ser2net reload` - restarts ser2net service
  - **Milestone 2 Complete!**

### Changed
- Moved documentation files to docs/ folder (AGENT_RULES.md, IMPLEMENTATION.md, DECISIONS.md)
- Fixed README.md and CLAUDE.md paths to reference docs/ folder

### Fixed
- Nothing yet

---

## Version History

_No releases yet. Development starting with Milestone 1._
