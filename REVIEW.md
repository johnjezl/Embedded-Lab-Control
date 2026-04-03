# Code Review Report — 2026-04-03

Comprehensive review of the Embedded Lab Control (labctl) codebase covering
defects, duplication, dead code, efficiency, memory leaks, architecture,
tests, and documentation.

## Fix Status

All critical (C1-C3), high priority (H1-H7), and medium priority (M1-M10,
excluding M3/M11 deferred) issues have been fixed.
- C1-C3: Fixed in commit a39f419
- H1-H6: Fixed in commit 3e0fc60
- H7: Fixed in commit 2d69d88
- M1, M2, M4, M5, M7, M10: Fixed in commit d1b5b11
- M3 (power status duplication): Deferred — larger refactor
- M6 (Flask teardown): Not needed — DB uses per-query connections
- M8, M9: Fixed with H3 in commit 3e0fc60
- M11 (API rate limiting): Deferred — needs design decision
- Tests added in commits 9563e3e, cfe1094, and subsequent

---

## CRITICAL DEFECTS

### C1. Missing `execute_query()` method — runtime crash
**File:** `src/labctl/core/manager.py` lines 691, 704, 823
The code calls `self.db.execute_query()` but the Database class has no such
method. Only `execute()`, `execute_one()`, `execute_insert()`, and
`execute_modify()` exist. Methods `get_status_history()` and `get_uptime()`
will crash at runtime.

**Fix:** Replace `execute_query()` with `execute()`.

### C2. Wrong row indexing in `get_status_history()` and `get_uptime()`
**File:** `src/labctl/core/manager.py` lines 717-725, 837-846
Uses tuple indexing (`row[0]`, `row[1]`) but `sqlite3.Row` requires
dict-style access (`row['status']`, `row['logged_at']`). Even if C1 is
fixed, these methods will produce wrong results or crash.

**Fix:** Change tuple indexing to dict-style access.

### C3. Open redirect vulnerability in auth
**File:** `src/labctl/web/auth.py` line 85-86
```python
next_url = request.args.get("next", url_for("views.index"))
return redirect(next_url)
```
The `next` parameter is not validated. Attacker can redirect to arbitrary
external URLs: `/login?next=https://attacker.com`.

**Fix:** Validate that `next_url` is a relative path (starts with `/` and
does not start with `//`).

---

## HIGH PRIORITY

### H1. Path traversal in SDWire file operations
**File:** `src/labctl/sdwire/controller.py` lines 208-255
`update_files()` uses `os.path.join(mount_point, dest_relative)` but does
not verify the resolved path stays under `mount_point`. A `dest_relative`
of `../../etc/passwd` could escape.

**Fix:** After `os.path.join`, verify `os.path.realpath(dest)` starts with
`os.path.realpath(mount_point)`. Apply to copies, renames, and deletes.

### H2. Missing null checks after `execute_one()` on freshly inserted rows
**File:** `src/labctl/core/manager.py` lines 266-267, 396-397, 501, 545, 572-573
Multiple methods call `SomeModel.from_row(row)` after an insert without
checking if `row` is None. If the retrieval fails, `from_row(None)` crashes.

**Fix:** Add `if not row: raise RuntimeError(...)` after each retrieval.

### H3. Kasa controller raises RuntimeError while others return bool
**Files:** `src/labctl/power/kasa.py` line 164, `src/labctl/power/tasmota.py`
line 42, `src/labctl/power/shelly.py` line 48
Kasa raises `RuntimeError` on failure, while Tasmota/Shelly return `False`.
This breaks polymorphism — callers must handle both patterns.

**Fix:** Standardize: all controllers should raise on error (preferred) or
all return bool. Update callers accordingly.

### H4. No timeout on `future.result()` in Kasa controller
**File:** `src/labctl/power/kasa.py` lines 135-137
`future.result()` blocks indefinitely if `asyncio.run()` hangs.

**Fix:** `result = future.result(timeout=self.timeout + 5)`

### H5. Thread safety race condition in SerialProxy
**File:** `src/labctl/serial/proxy.py` lines 420-425, 498, 520-521
`self.clients` dict iterated in `_broadcast()` while other coroutines may
modify it. `self.writer_client_id` has a TOCTOU race between check and set.

**Fix:** Use `asyncio.Lock()` to protect shared state. Copy dict before
iteration: `list(self.clients.items())`.

### H6. Orphaned asyncio task in proxy
**File:** `src/labctl/serial/proxy.py` line 492
`asyncio.create_task(self.stop())` creates a fire-and-forget task that may
complete during `_read_serial_loop()` execution, causing unhandled exceptions.

**Fix:** Store task reference and guard with state check.

### H7. MCP power_cycle docstring says 2.0s but default is 3.0s
**File:** `src/labctl/mcp_server.py` line 340 (docstring)

**Fix:** Update docstring to say 3.0.

---

## MEDIUM PRIORITY

### M1. N+1 query problem in SBC loading
**File:** `src/labctl/core/manager.py` lines 102-110, 164-169
`_load_sbc_relations()` makes 4-5 queries per SBC. `list_sbcs()` calls it
in a loop, producing N*5 queries for N SBCs. Serial device loading adds
another N+1.

**Fix:** Use JOIN queries to batch-load relations. Extract serial device
loading to a helper method to eliminate the 3x duplication at lines
102-110, 426-433, 444-450.

### M2. Duplicated SBC-to-dict serialization
**Files:** `src/labctl/mcp_server.py` lines 55-104 (`_sbc_to_dict`),
`src/labctl/web/api.py` lines 13-62 (`sbc_to_dict`)
Near-identical functions in two files. Changes to SBC schema require
updating both.

**Fix:** Create a single `sbc_to_dict()` in models.py or a shared module.

### M3. Duplicated power status checking pattern
**Files:** `mcp_server.py` lines 130-153, `api.py` lines 158-180,
`views.py` lines 44-50, `cli.py` lines 1760-1780
Same try/except pattern for getting power state repeated 4+ times.

**Fix:** Extract to shared helper, e.g., `get_power_state(sbc) -> dict`.

### M4. Non-atomic upsert pattern
**File:** `src/labctl/core/manager.py` lines 377-389, 476-488, 526-534, 627-634
Uses DELETE then INSERT instead of SQLite's `INSERT ... ON CONFLICT`.
Not atomic; a crash between DELETE and INSERT loses data.

**Fix:** Use `INSERT ... ON CONFLICT DO UPDATE SET ...`.

### M5. Broad exception swallowing in config loading
**File:** `src/labctl/core/config.py` lines 317-318
`except Exception: continue` silently ignores YAML parse errors, permission
errors, etc. No logging.

**Fix:** Log a warning before continuing.

### M6. Missing Flask teardown handler for ResourceManager
**File:** `src/labctl/web/app.py` lines 67-72
`g.manager` created in `before_request` but no `teardown_request` to clean
up database connections.

**Fix:** Add `@app.teardown_request` handler.

### M7. File handle leak in SessionLogger rotation
**File:** `src/labctl/serial/proxy.py` lines 194-207, 313-366
If rotation fails between closing old file and opening new file, the handle
is lost. Subsequent `log_output()` calls fail silently.

**Fix:** Wrap in try/except, ensure new handle is always created or error
is logged.

### M8. Bare exception handling in power controllers
**Files:** `src/labctl/power/tasmota.py` line 41,
`src/labctl/power/shelly.py` line 48, `src/labctl/power/kasa.py` line 148
`except Exception` silently swallows all errors including network timeouts.

**Fix:** Log the exception: `logger.warning(f"... failed: {e}")`.

### M9. Missing f-string space in Kasa error message
**File:** `src/labctl/power/kasa.py` lines 160-161
Produces `"192.168.1.100[1]"` instead of `"192.168.1.100 [1]"`.

**Fix:** Add space before `[`.

### M10. Inconsistent ser2net format strings
**File:** `src/labctl/serial/ser2net.py` lines 32-33 vs 86-87
`to_ser2net_dict()` and `generate_from_mapping()` use different baud format
strings. Line 87 is missing comma separator and has wrong field order.

**Fix:** Align formats and extract parity mapping to module constant.

### M11. No rate limiting on power control API
**File:** `src/labctl/web/api.py` lines 183-236
POST to `/sbcs/<name>/power` has no rate limiting. Rapid cycling could
damage hardware.

**Fix:** Add rate limiting (e.g., min 5s between cycles per SBC).

---

## LOW PRIORITY

### L1. Unused `_connection` field in Database
**File:** `src/labctl/core/database.py` line 150
`self._connection: Optional[sqlite3.Connection] = None` is initialized but
never set or used.

**Fix:** Remove it.

### L2. Redundant `is True` checks in Shelly/Tasmota
**Files:** `src/labctl/power/shelly.py` line 59,
`src/labctl/power/tasmota.py` line 60
`return result.get("ison", False) is True` — the `is True` is unnecessary.

**Fix:** `return bool(result.get("ison", False))`

### L3. Log file timestamp collision risk
**File:** `src/labctl/serial/proxy.py` lines 174, 203
Timestamps use seconds precision. Two rotations in the same second
overwrite. Add milliseconds.

### L4. WebSocket console bridge is incomplete
**File:** `src/labctl/web/websocket.py` lines 102, 124-128
Stubbed with comments. Only echo functionality, no real TCP bridge.

### L5. Potential KeyError on invalid parity value
**File:** `src/labctl/serial/ser2net.py` lines 32, 86
Direct dict lookup without `.get()` or validation.

### L6. Missing validation on MCP tool parameters
**File:** `src/labctl/mcp_server.py`
- `power_cycle()`: `delay` can be negative
- `boot_test()`: `runs` can be 0 or negative, `timeout` can be 0

### L7. Inconsistent CLI option naming
**File:** `src/labctl/cli.py`
Mix of `--tcp-port`, `--baud`, `--serial-device`. Should standardize.

### L8. God functions in CLI
**File:** `src/labctl/cli.py`
`log_cmd()` ~150 lines, `boot_test_cmd()` ~130 lines. Extract helpers.

### L9. CLAUDE.md says "schema v2" but current is v3
**File:** `CLAUDE.md` line 32

---

## TEST ISSUES

### T1. Missing test coverage areas
- Health checker: no tests for serial check timeout, rapid consecutive checks
- Boot test: no tests for deploy_fn or power_cycle_fn exceptions
- Database: no concurrent migration or partial failure tests
- Manager: no concurrent assignment tests
- MCP server: no timeout tests for power controllers
- Web auth: no session timeout, concurrent session, or invalid CSRF format tests
- CLI integration: only covers `--version`, `--help`, `--delay`

### T2. Fragile timing-dependent tests
- `test_serial_capture.py` line 251: `test_capture_pattern_in_partial_line`
  uses manual threading with 0.1s delay. May fail on slow systems.
- `FakeTCPServer` socket operations lack synchronization guarantees.

### T3. Duplicated test setup
- `test_manager.py`: each test class creates SBCs independently
- `test_mcp_server.py`: `populated_manager` fixture duplicated across classes
- `test_web.py`: fixtures could use `scope="module"` for efficiency

### T4. Weak or missing assertions
- `test_config.py` env override tests: don't test precedence
- `test_ser2net.py` `test_empty_ports_list`: doesn't verify no connection blocks
- `test_boot_test.py`: doesn't verify call ordering (deploy before power cycle)

### T5. Incorrect mocking
- `test_mcp_server.py` SDWire update reboot: doesn't verify `from_plug`
  called with correct power_plug
- `test_health.py`: doesn't verify timeout parameter passed to subprocess

---

## DOCUMENTATION ISSUES

### D1. Outdated information
- CLAUDE.md line 32: says "schema v2", should be v3
- README.md: test count (322) is stale, now 378+
- MCP_SERVER.md: tool list incomplete (missing serial_capture, serial_send, boot_test were just added but verify)

### D2. Missing documentation
- No docs on max SBC count or performance characteristics
- No docs on serial proxy memory usage
- No failure mode documentation (what happens when ser2net is down?)
- No docs on sudoers setup for SDWire operations
- deploy-config.json format only documented in skill file, not README

### D3. Inconsistencies between docs
- README vs MCP_SERVER.md tool lists don't match
- deploy-and-test.md says "regex" for success_pattern but isn't clear
  whether literal string or regex is expected
- Partition numbering: 0-based or 1-based not clarified

### D4. Incomplete sections
- README user management: no edit/delete docs
- STATUS.md blockers: missing known limitations
- README quick start: missing verification steps

---

## SUMMARY

| Category | Critical | High | Medium | Low |
|----------|----------|------|--------|-----|
| Defects | 3 | 7 | 11 | 9 |
| Tests | - | - | 5 areas | - |
| Docs | - | - | 4 areas | - |

**Recommended fix order:**
1. C1-C3 (critical bugs and security)
2. H1-H2 (security and crash prevention)
3. H3-H7 (reliability)
4. M1-M4 (performance and architecture)
5. Everything else
