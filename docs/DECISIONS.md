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
