cat > CLAUDE.md << 'EOF'
# Embedded Lab Control (labctl)

## Project Overview
Centralized management system for embedded development lab resources. Provides deterministic USB serial access, power control, and resource tracking for multiple SBCs.

## Key Documentation
- `docs/ARCHITECTURE.md` - System design and component specs
- `docs/IMPLEMENTATION.md` - Milestone TODOs (update status here!)
- `docs/AGENT_RULES.md` - **READ THIS FIRST** - Development process rules
- `docs/DECISIONS.md` - Design decision log
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
Starting Milestone 1: Foundation

## Tech Stack
- Python 3.10+
- Click (CLI)
- Flask (web/API)
- SQLite (database)
- ser2net (serial over TCP)
- pytest (testing)

## Commands Reference
```bash
# Run tests
pytest tests/ -v

# Format code
black src/ tests/
isort src/ tests/

# Lint
flake8 src/ tests/
```
EOF
```

Then commit this with the initial commit.

---

**Suggested starting prompt for Claude Code:**
```
Read docs/AGENT_RULES.md and docs/IMPLEMENTATION.md to understand the project process and current state. Then begin Milestone 1, starting with sub-milestone 1.1 (Project Setup). Follow the rules of engagement - report back before committing anything.
