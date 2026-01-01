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

### Changed
- Moved documentation files to docs/ folder (AGENT_RULES.md, IMPLEMENTATION.md, DECISIONS.md)
- Fixed README.md and CLAUDE.md paths to reference docs/ folder

### Fixed
- Nothing yet

---

## Version History

_No releases yet. Development starting with Milestone 1._
