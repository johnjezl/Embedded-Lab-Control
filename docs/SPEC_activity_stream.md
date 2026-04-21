# SPEC: Activity Stream

Specification for a unified, structured, append-only stream of every
state-changing action labctl performs — viewable live (CLI tail, web)
and queryable historically.

**Status:** Phase A, Phase B, and Phase C implemented
**Source request:** observability gap surfaced during monitor-daemon
outage triage (2026-04-17 — daemon crashed silently on every cycle for
hours before being noticed)
**Target release:** v0.x

---

## Problem

Mutations to lab state come from four independent front-ends (CLI,
MCP, web API, monitor daemon), and there is no unified way to observe
what is happening. Today's symptoms:

- The monitor daemon crashed silently on every cycle for an unknown
  period. A live stream would have immediately flagged "last
  successful cycle: 3h ago."
- No way to tell which AI agent just power-cycled an SBC, or whether
  a flash happened.
- Status changes surface only by polling `labctl status` or reading
  the systemd journal.
- Debugging "why is the board in this state?" requires cross-
  referencing journal logs across three services.

## Goals

- Every state-changing action across CLI, MCP, web API, and the
  monitor daemon is recorded as a single structured event.
- Live stream viewable via `labctl activity tail --follow` and a
  `/activity` page on the web dashboard.
- Historical queries filterable by SBC, actor, source, and time
  range.
- AI agents can observe recent activity via future MCP resources
  planned for a later phase.
- Attribution: every event carries the originating actor (`cli:john`,
  `mcp-stdio:12345-...`, `web:alice`, `daemon`).

## Out of scope (v1)

- Read operations (`list`, `show`, `health-check` without `--update`),
  MCP resource reads, `GET` API calls.
- Tamper-evident / signed audit logs.
- Export to external SIEMs (NDJSON export in Phase D covers the
  common case).

## Scope — what emits events

Instrumentation lives at two layers:

1. **Low-level (ResourceManager, PowerController, SDWireController)**
   — catches every mutation regardless of which front-end triggered
   it, including code paths that may be added later.
2. **Front-end boundary (CLI, MCP, web)** — sets `actor` and `source`
   via contextvars before calling down into the manager, so every
   emitted event is correctly attributed.

Reads are not emitted. If we ever need that, it goes behind a
`--audit-reads` verbose flag in a later phase.

### Event sources

| Source   | Actor format                          | What emits                                      |
|----------|---------------------------------------|-------------------------------------------------|
| CLI      | `cli:<user>`                          | Every mutating subcommand                       |
| MCP      | `mcp-stdio:<pid>-<epoch>` (matches claims) | Every `@mcp.tool()` that mutates            |
| Web/API  | `web:<user>` / `api:<user>`           | Every POST/PUT/PATCH/DELETE handler             |
| Daemon   | `daemon`                              | Status transitions, alerts triggered            |
| Internal | `internal`                            | Fallback when no boundary context is active     |

## Event schema

An `audit_log` table already exists (introduced pre-v1 for manager
mutations). We extend it in schema v5 rather than creating a new
table — one source of truth, all existing manager-level emits
automatically pick up the new columns:

```sql
-- Existing columns (unchanged):
--   id, action, entity_type, entity_id, entity_name, details, logged_at

-- New columns in schema v5:
ALTER TABLE audit_log ADD COLUMN actor      TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE audit_log ADD COLUMN source     TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE audit_log ADD COLUMN result     TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE audit_log ADD COLUMN claim_id   INTEGER REFERENCES claims(id);

CREATE INDEX idx_audit_log_logged_at ON audit_log(logged_at DESC);
CREATE INDEX idx_audit_log_actor     ON audit_log(actor, logged_at DESC);
CREATE INDEX idx_audit_log_source    ON audit_log(source, logged_at DESC);
```

Field mapping from the conceptual model above:

| Spec field  | Column                     |
|-------------|----------------------------|
| `timestamp` | `logged_at` (ms precision when written by audit.emit) |
| `actor`     | `actor`                    |
| `source`    | `source`                   |
| `action`    | `action`                   |
| `target`    | `entity_name`              |
| (entity id) | `entity_id`                |
| (entity kind) | `entity_type`            |
| `result`    | `result`                   |
| `details`   | `details` (JSON)           |
| `claim_id`  | `claim_id`                 |

`details` is kept small (≤ 4 KB, truncated otherwise).

### Action names

Canonical verb_noun form. Examples:

- `power_on`, `power_off`, `power_cycle`
- `sdwire_to_host`, `sdwire_to_dut`, `sdwire_update`, `flash_image`
- `claim_sbc`, `release_sbc`, `renew_sbc_claim`, `force_release_sbc`
- `add_sbc`, `update_sbc`, `remove_sbc`
- `assign_serial_port`, `remove_serial_port`, `assign_power_plug`,
  `set_network_address`
- `serial_send`, `boot_test`
- `status_change` (daemon — when an SBC transitions status)

### Redaction

Before emit, `details` is redacted:

- Strip `password`, `token`, `api_key`, `ssh_key`, `session_cookie`.
- Serial buffer payloads (`serial_send` data, flashed image contents)
  truncated to head + tail 256 bytes with a middle-elided marker.

## Delivery channels

### 1. CLI

```
labctl activity tail [--follow] [--sbc X] [--actor X] [--source mcp]
                     [--since 5m|2h|1d] [--limit N]
```

Human-readable line format:

```
10:22:01.543  mcp-stdio:12345  power_on         pi-5-1   ok      (2.1s)
10:22:04.112  cli:john         sdwire_to_host   pi-5-2   forbidden  claim held by agent-alice
10:22:04.310  daemon           status_change    pi-5-2   ok      online -> offline
```

`--follow` polls the DB every 500 ms. This keeps the CLI usable even
when the web service is stopped.

### 2. Web dashboard

New `/activity` page:

- Live event feed via Server-Sent Events from `/activity/stream`.
- Filter chips: source and result.
- Color-coded: green = ok, red = error, yellow = forbidden,
  gray = daemon.
- Last 200 events on page load, stream after.

### 3. REST API

- `GET /api/activity?since=<ts>&limit=N&sbc=X&actor=X&source=X`

### 4. MCP resource

- `lab://activity/recent` — last 50 events (agent gets a quick "what
  just happened" snapshot).
- `lab://activity/{sbc_name}` — last 50 events targeting that SBC.

Status: not yet implemented; still planned for a later phase.

## Performance and retention

- Each `emit()` is a single INSERT (~0.5 ms at lab scale ≈ 10
  events/sec sustained worst case).
- SSE fanout is queue-based per subscriber and driven by the
  background broadcaster thread.
- Events older than 30 days are pruned by the existing claim-sweep
  worker (one more `DELETE` each sweep).
- `details` JSON truncated to 4 KB before insert.

## Plumbing — boundary context

A `contextvars.ContextVar` for each of `_actor`, `_source`,
`_claim_id`. Each front-end sets them on entry:

```python
# src/labctl/audit/context.py
from contextlib import contextmanager

@contextmanager
def activity_context(actor: str, source: str, claim_id: int | None = None):
    token_a = _actor.set(actor)
    token_s = _source.set(source)
    token_c = _claim_id.set(claim_id)
    try:
        yield
    finally:
        _actor.reset(token_a)
        _source.reset(token_s)
        _claim_id.reset(token_c)
```

- **CLI**: wraps `main()` — `activity_context(f"cli:{getuser()}", "cli")`.
- **MCP**: wraps each tool call — `activity_context(session_id, "mcp")`.
  Reads session ID from the existing MCP session-tracking used by
  claims.
- **Web**: Flask `before_request` sets context from the session user.
  When auth is disabled, anonymous mutations are attributed as
  `web:anonymous` / `api:anonymous`.
- **Daemon**: sets `activity_context("daemon", "daemon")` for its
  thread loop.
- **Default**: if nothing is set (direct Python use, tests), events
  are attributed to `internal`.

## Phases

### Phase A — foundation

- `labctl.audit` module: `emit()`, contextvars, `activity_context()`
  context manager, redaction.
- Schema v5 migration (extend `audit_log` with actor/source/result and
  claim columns + indexes).
- Wire into `ResourceManager` mutating methods (`create_sbc`,
  `update_sbc`, `remove_sbc`, `assign_serial_port`,
  `remove_serial_port`, `set_network_address`, `remove_network_address`,
  `assign_power_plug`, `remove_power_plug`, `assign_sdwire`,
  `unassign_sdwire`, claim ops).
- Wire into `PowerController.power_on/off/cycle`.
- Wire into `SDWireController.to_host/to_dut/flash_image/update_files`.
- CLI boundary sets context in `main()`.
- `labctl activity` CLI query command (no `--follow` yet; tail the
  DB once and exit).
- Tests: emit paths, redaction, context propagation, schema migration.

Deliverable: every mutation is recorded to the DB and queryable via
CLI.

### Phase B — live

- Server-Sent Events (SSE) at `/activity/stream` — single long-lived
  HTTP response that yields events as they arrive. Chose SSE over
  Flask-SocketIO because the existing SocketIO scaffolding was never
  wired in and integrating it cleanly would require switching the
  WSGI runner to eventlet/gevent. SSE works with the current plain
  `app.run()`, is natively supported by browsers via `EventSource`,
  and is one-way (which is all the activity feed needs).
- `ActivityBroadcaster` background thread polls `audit_log` every
  500ms and fans new rows out to per-subscriber queues. Because the
  broadcaster polls the DB (rather than hooking `emit()` directly),
  events from CLI, MCP, and the daemon — which run in separate
  processes — all reach the browser.
- `/activity` web page with live feed, source + result filter chips,
  and server-rendered hydration of the last 200 events so the page is
  usable before the first SSE frame arrives.
- `GET /api/activity` — JSON query endpoint for programmatic access
  (supports the same filters as the CLI).
- `labctl activity tail --follow` polls the DB directly (not SSE).
  This keeps the CLI fully functional when the web service is stopped
  and avoids baking an HTTP client into the CLI for this one feature.

### Phase C — source fidelity

- Explicit `activity_context()` wrapping in each MCP tool and REST
  handler so `actor` and `source` reflect the originating agent/user,
  not `internal`.
- Tests asserting actor/source for each layer.

Status: implemented.

### Phase D — polish

- `lab://activity/*` MCP resources.
- 30-day retention sweep.
- NDJSON export: `labctl activity export --format ndjson --since Xd`.

## Testing

Phase A test coverage:

- `audit.emit()` writes the expected row with all fields populated.
- `activity_context` correctly sets and resets contextvars (including
  nesting).
- Redaction strips known sensitive keys.
- Large `details` truncates to 4 KB without raising.
- Schema v5 migration applied idempotently (migrate-from-v4 on a real
  fixture DB).
- Every instrumented manager method emits on success and on failure.
- CLI `labctl activity` filters by SBC, actor, source, result, and
  time range.
- CLI `labctl activity tail` and web `/api/activity`, `/activity`, and
  `/activity/stream` surfaces render the recorded events correctly.

## Follow-up TODOs

Tracked follow-ups discovered during or after Phase B. Not in any
phase commitment yet — pick up as bandwidth allows.

- **Reverse-chronological ordering (newest-first).**
  - CLI `activity tail` currently prints oldest-first (tail-of-file
    semantics); switch to newest-first so the most recent event is
    the first thing you see. Consider `--oldest-first` as an opt-in
    escape hatch for users who want tail-like scrolling.
  - Web `/activity` has a subtle bug in the hydration path: the
    server sends the last 200 events oldest-first, the JS iterates
    newest→oldest and calls `feed.prepend()` each time, which ends
    up with oldest at the TOP of the feed. Live events then prepend
    above that, producing mixed ordering. Fix: build the DOM in a
    single pass that preserves newest-at-top invariant.

- **Timezone-aware timestamps.**
  - Timestamps are stored in the server's local time without a
    timezone marker. Browsers displaying the page may be in a
    different timezone. Detect the browser TZ with
    `Intl.DateTimeFormat().resolvedOptions().timeZone` and format
    timestamps client-side in local time. Fall back to the server
    TZ (pass it with the initial render) when the browser value is
    unavailable.
  - CLI keeps server TZ formatting as-is.
  - Longer-term, consider storing UTC ISO-8601 with explicit `Z`
    so any consumer can render without guessing.

## Open questions (resolved)

1. **Daemon noise** — emit every cycle or only on status change?
   **Resolved:** only status changes.
2. **Separate table or unify with `status_history` / `claim_history`?**
   **Resolved:** separate — those tables are row-per-entity with a
   history; this is an append-only firehose across all entities.
3. **Include reads?** **Resolved:** no in v1.
