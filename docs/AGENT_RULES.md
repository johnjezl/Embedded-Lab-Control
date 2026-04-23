# Lab Controller System - Agent Rules of Engagement

## Purpose

This document defines the processes, standards, and checkpoints that govern autonomous agent development on this project. Following these rules ensures consistent progress, maintainable code, and appropriate human oversight.

---

## Icon Key for TODO Items

| Icon | Meaning |
|------|---------|
| ☐ | Not started |
| ✅ | Complete |
| ⏸️ | Deferred to later phase |
| 🔗 | Has dependency on another milestone (shown as ⏸️🔗 or ☐🔗) |

---

## 1. Development Workflow

### 1.1 Session Startup Protocol

At the start of each development session, the agent MUST:

1. **Review current state**
   - Read `IMPLEMENTATION.md` to identify current milestone and next TODO
   - Check `CHANGELOG.md` for recent changes
   - Review any open issues or blockers noted in `STATUS.md`

2. **Confirm context**
   - State the current milestone and sub-task to the user
   - Confirm understanding before proceeding

3. **Verify environment**
   - Confirm project directory exists and is accessible
   - Verify git status (clean working tree or note uncommitted changes)
   - Check that tests pass before making changes

### 1.2 Task Execution Flow

For each numbered sub-milestone item:

```
┌─────────────────────────────────────────────────────────────┐
│  1. Read and understand the requirement                      │
│  2. Design approach (document if non-trivial)               │
│  3. Implement with inline comments                          │
│  4. Write/update tests                                      │
│  5. Run tests - ALL must pass                               │
│  6. Update documentation                                    │
│  7. Update TODO status in IMPLEMENTATION.md                 │
│  8. Report completion to user                               │
│  9. Await user approval before staging/committing           │
│  10. Stage and commit (only after approval)                 │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 Completion Criteria

A task is NOT complete until:

- ☐ Code is implemented and functional
- ☐ Unit tests written and passing
- ☐ Integration tests updated if applicable
- ☐ Inline code comments added for non-obvious logic
- ☐ IMPLEMENTATION.md TODO item marked with ✅
- ☐ CHANGELOG.md updated with change summary
- ☐ User has approved the changes

---

## 2. Testing Requirements

### 2.1 Test-Driven Development (Preferred)

When practical, write tests BEFORE implementation:

1. Write failing test that defines expected behavior
2. Implement minimum code to pass test
3. Refactor while keeping tests green

### 2.2 Test Coverage Requirements

| Component Type | Required Tests |
|----------------|----------------|
| Database operations | Unit tests for all CRUD operations |
| Business logic | Unit tests for all public functions |
| CLI commands | Integration tests for each command |
| REST API endpoints | Integration tests for each endpoint |
| Power controllers | Unit tests with mocked HTTP calls |
| Serial operations | Unit tests with mocked serial ports |

### 2.3 Test Execution

**Before ANY commit:**
```bash
# Run full test suite
pytest tests/ -v

# Check coverage (aim for >80%)
pytest tests/ --cov=labctl --cov-report=term-missing
```

**Tests MUST pass before:**
- Marking a TODO as ✅
- Requesting commit approval
- Moving to next sub-milestone

### 2.4 Test File Organization

```
tests/
├── conftest.py           # Shared fixtures
├── unit/
│   ├── test_database.py
│   ├── test_models.py
│   ├── test_power_tasmota.py
│   └── ...
├── integration/
│   ├── test_cli.py
│   ├── test_api.py
│   └── ...
└── fixtures/
    ├── sample_config.yaml
    └── test_database.db
```

---

## 3. Version Control Practices

### 3.1 Commit Approval Gate

**CRITICAL: The agent MUST NOT stage or commit without explicit user approval.**

Workflow:
1. Agent completes sub-milestone
2. Agent reports to user:
   - Summary of changes
   - Files modified
   - Test results
   - Proposed commit message
3. Agent WAITS for user approval
4. Only after approval: `git add -A && git commit -m "..."`

### 3.2 Commit Granularity

Commit after completing each **numbered sub-milestone** (e.g., 1.1, 1.2, 1.3).

**Good commit scope:**
- One logical unit of work
- All related tests included
- Documentation updated

**Bad commit scope:**
- Multiple unrelated changes
- Incomplete feature
- Failing tests

### 3.3 Commit Message Format

```
<type>(<scope>): <short description>

<body - what and why>

Milestone: M<n>.<sub>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `test`: Adding/updating tests
- `refactor`: Code change that neither fixes nor adds
- `chore`: Build, config, tooling changes

**Example:**
```
feat(cli): implement labctl ports command

Add CLI command to list all serial ports under /dev/lab/.
Displays device path, symlink name, and ser2net port if configured.

Milestone: M1.4
```

### 3.4 Branch Strategy

- `main` - stable, tested code only
- `dev` - active development (agent works here)
- Feature branches optional for complex features

```bash
# Agent typically works on dev branch
git checkout dev

# Merge to main only with user approval after milestone completion
```

### 3.5 Push Policy

**Do NOT push without user approval.**

After commit approval, user will decide when to push:
```bash
git push origin dev
```

---

## 4. Documentation Requirements

### 4.1 Living Documents

These files MUST be kept current:

| File | Updated When |
|------|--------------|
| `IMPLEMENTATION.md` | TODO status changes |
| `CHANGELOG.md` | After each commit |
| `STATUS.md` | Session start/end, blockers found |
| `README.md` | User-facing features added |
| `docs/ARCHITECTURE.md` | Design decisions change |

### 4.2 CHANGELOG.md Format

```markdown
# Changelog

## [Unreleased]

### Added
- labctl ports command for listing serial devices (M1.4)

### Changed
- Updated config loader to support environment variables (M1.5)

### Fixed
- Fixed ser2net config generation for ACM devices (M1.3)

## [0.1.0] - YYYY-MM-DD
...
```

### 4.3 STATUS.md Format

```markdown
# Project Status

## Current State
- **Milestone**: M1 - Foundation
- **Sub-task**: 1.4 - Basic CLI Structure
- **Status**: In Progress

## Last Session
- **Date**: YYYY-MM-DD
- **Completed**: 1.1, 1.2, 1.3
- **Commits**: abc1234, def5678

## Blockers
- None currently

## Notes
- Decided to use Click instead of argparse for CLI
- USB hub 2 has flaky port 3, avoid using
```

### 4.4 Inline Code Documentation

**Required comments:**
- Module docstrings explaining purpose
- Function docstrings with args/returns
- Non-obvious logic explanations
- TODO markers for known improvements
- FIXME markers for known issues

**Example:**
```python
"""
Serial port manager for lab controller.

Handles ser2net configuration generation and serial port discovery.
"""

def generate_ser2net_config(ports: List[SerialPort]) -> str:
    """
    Generate ser2net YAML configuration from port assignments.
    
    Args:
        ports: List of SerialPort objects with device paths and TCP ports
        
    Returns:
        YAML string suitable for /etc/ser2net.yaml
        
    Note:
        Requires ser2net 4.0+ for YAML config format
    """
    # TODO: Add support for custom serial options per port
    ...
```

### 4.5 Decision Log

For significant design decisions, document in `docs/DECISIONS.md`:

```markdown
# Decision Log

## D001: Use physical USB path for udev rules
- **Date**: YYYY-MM-DD
- **Context**: Need deterministic device naming
- **Options**: Serial number vs physical path
- **Decision**: Physical path
- **Rationale**: USB hubs lack serial numbers; path is intuitive
```

---

## 5. Error Handling and Recovery

### 5.1 When Tests Fail

1. **STOP** - Do not proceed to next task
2. Analyze failure - understand root cause
3. Fix the issue
4. Re-run full test suite
5. Only proceed when ALL tests pass

### 5.2 When Blocked

If unable to proceed due to:
- Missing information
- External dependency
- Design question
- Hardware issue

**Action:**
1. Document blocker in `STATUS.md`
2. Mark TODO as ⏸️ or ⏸️🔗
3. Report to user immediately
4. Suggest alternatives if possible
5. Move to next unblocked task if available

### 5.3 When Mistakes Are Made

If a bug is introduced or wrong approach taken:

1. **Do not hide it** - Report to user
2. Document what went wrong
3. Propose fix or rollback
4. Get user approval for recovery plan
5. Add test to prevent recurrence

---

## 6. Code Quality Standards

### 6.1 Style Guide

- Follow PEP 8 for Python code
- Use `black` for formatting (line length 88)
- Use `isort` for import ordering
- Use `flake8` for linting

```bash
# Before requesting commit approval
black src/ tests/
isort src/ tests/
flake8 src/ tests/
```

### 6.2 Type Hints

All public functions MUST have type hints:

```python
def assign_port(sbc_name: str, port_type: str, device_path: str) -> bool:
    ...
```

### 6.3 Error Handling

- Use specific exceptions, not bare `except:`
- Log errors with context
- Provide meaningful error messages to users
- Don't silently swallow errors

### 6.4 Security

- No hardcoded credentials
- No secrets in code or commits
- Validate all external input
- Use parameterized SQL queries

---

## 7. Communication Protocol

### 7.1 Progress Reporting

At natural checkpoints, report:
- What was just completed
- Current test status
- Next planned action
- Any concerns or questions

### 7.2 Requesting Approval

When requesting commit approval, provide:

```
## Commit Approval Request

**Sub-milestone**: 1.4 - Basic CLI Structure

**Changes**:
- Created src/labctl/cli.py with Click framework
- Implemented `labctl ports` command
- Implemented `labctl connect` command
- Added tests/integration/test_cli.py

**Files Modified**:
- src/labctl/cli.py (new)
- src/labctl/__init__.py (modified)
- tests/integration/test_cli.py (new)
- pyproject.toml (modified - added click dependency)
- IMPLEMENTATION.md (updated TODOs)
- CHANGELOG.md (updated)

**Test Results**:
- All 12 tests passing
- Coverage: 87%

**Proposed Commit Message**:
feat(cli): implement basic CLI structure with ports and connect commands

Milestone: M1.4

**Ready to commit?** Awaiting your approval.
```

### 7.3 Asking Questions

When clarification is needed:
- Be specific about what's unclear
- Provide options if possible
- Explain impact of different choices
- Wait for response before proceeding

### 7.4 Session Handoff

At end of session or when stopping:

1. Ensure working tree is clean (committed or stashed)
2. Update `STATUS.md` with current state
3. Note any in-progress work
4. List next steps
5. Push if approved

---

## 8. Milestone Completion Checklist

Before declaring a milestone COMPLETE:

- ☐ All sub-milestone TODOs marked ✅
- ☐ All tests passing
- ☐ Documentation updated
- ☐ CHANGELOG updated
- ☐ Code reviewed with user
- ☐ All commits pushed (with approval)
- ☐ User confirms milestone acceptance
- ☐ Create git tag: `git tag -a vM<n>.0 -m "Milestone <n> complete"`

---

## 9. File Modification Rules

### 9.1 Files Agent CAN Modify Freely

- Source code in `src/`
- Tests in `tests/`
- `CHANGELOG.md`
- `STATUS.md`
- `IMPLEMENTATION.md` (TODO status only)

### 9.2 Files Requiring Discussion First

- `ARCHITECTURE.md` (design changes)
- `pyproject.toml` (new dependencies)
- Configuration schemas
- Database schema changes
- Public API changes

### 9.3 Files Agent Should NOT Modify

- `.git/` directory
- User configuration files
- System files outside project
- Credentials or secrets

---

## 10. Tool Usage

### 10.1 Preferred Tools

| Task | Tool |
|------|------|
| Testing | pytest |
| Formatting | black |
| Import sorting | isort |
| Linting | flake8 |
| Type checking | mypy (optional) |
| Coverage | pytest-cov |

### 10.2 Commands Reference

```bash
# Run tests
pytest tests/ -v

# Run tests with coverage
pytest tests/ --cov=labctl --cov-report=term-missing

# Format code
black src/ tests/
isort src/ tests/

# Lint
flake8 src/ tests/

# Git status
git status
git diff --stat

# Stage and commit (ONLY after approval)
git add -A
git commit -m "message"
```

---

## 11. Hardware Claims (Exclusive Access)

When multiple agents work against the same lab hardware, use the claims
system to prevent collisions. See `docs/RESOLVED_SPEC_claims.md` for the full
design.

### 11.1 Agent Workflow

1. **Claim before multi-step operations:**
   ```
   labctl claim <sbc> --duration 30m --reason "GPU bringup"
   # or via MCP:
   claim_sbc(sbc_name="...", duration_minutes=30, reason="...")
   ```
2. **Mutating operations are gated:** `power_on`, `serial_send`,
   `sdwire_update`, `flash_image`, `boot_test`, etc. return structured
   errors if another agent holds the claim.
3. **Heartbeat is automatic:** every tool call (reads and writes) by the
   claimant extends the deadline.
4. **Release when done:** `labctl release <sbc>` or `release_sbc(...)`.
   Claims also auto-expire and are released when the MCP session exits.
5. **Check pending release requests:** if another agent needs the board,
   they'll call `request_sbc_release`; you'll see the notice on your
   next tool call.

### 11.2 Agent Name Convention

Declare a descriptive `agent_name` on first claim:
- `"jetson-gpu-agent"`, `"pi5-smp-dispatch"`, `"boot-test-runner"`
- Appears in `labctl status` and audit log
- Unnamed agents get `unnamed-<session-hash>` — functional but opaque

### 11.3 Duration Guidelines

| Workflow | Duration |
|----------|----------|
| Quick power cycle / flash | 5–15m |
| Build + deploy + boot test | 30m–1h |
| Overnight reliability run | 8–24h |

Max is configurable (default 24h). Heartbeat extends the deadline
automatically.

---

## Summary: The Golden Rules

1. **Test before you commit** - All tests must pass
2. **Document as you go** - Don't defer documentation
3. **Never commit without approval** - Always check in first
4. **Update TODOs immediately** - Keep IMPLEMENTATION.md current
5. **Report blockers fast** - Don't spin on problems
6. **Small, focused commits** - One sub-milestone per commit
7. **Communicate clearly** - State what you did, what's next
8. **When in doubt, ask** - User oversight is a feature, not a bug
