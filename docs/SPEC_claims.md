# SPEC: Hardware Claims (Exclusive Access Coordination)

Specification for adding exclusive-access claim tracking to labctl.
Allows AI agents and humans to reserve SBCs for extended periods,
preventing destructive interference between concurrent workflows.

**Status:** Specification / pending implementation
**Source request:** SLM-OS capstone project (multi-agent coordination pain)
**Target release:** v0.x

---

## Problem

When multiple AI agents (or humans) work simultaneously against the
same lab hardware, they collide. Scenarios observed in practice:

- Agent A flashes a kernel via `sdwire_update`; Agent B overwrites it
  seconds later with a different kernel; Agent A's subsequent
  `boot_test` reports results for Agent B's kernel.
- Both agents open `serial_capture` on the same port — the shared
  ser2net multi-client design allows this, but output interleaves
  confusingly. Worse, concurrent `serial_send` calls mix command bytes
  on the wire.
- One agent runs `boot_test --count 50` (overnight reliability run);
  another power-cycles the board mid-iteration, silently corrupting
  the test.
- An agent crashes without cleanup; other agents have no way to know
  the board is actually free again.

Current mitigation is manual: humans add "unavailable" notes to agent
memory systems or chat channels. This is reactive, error-prone, and
doesn't survive agent restarts.

## Goals

- Agents can reserve an SBC for a declared duration with a
  human-readable reason
- Non-claimant operations against a claimed SBC fail cleanly with
  structured information about who owns it and when it expires
- Claims expire automatically — crashed/abandoned agents don't hold
  resources forever
- Human operators can always override via force-release
- Existing single-agent workflows are not disrupted: short ad-hoc
  operations continue to work without ceremony
- Backward compatible: unauthenticated / unclaimed operations still
  succeed when no claim is held

## Non-Goals

- Fine-grained locking below the SBC level (serial-only, SD-only,
  power-only). The SBC is the locking unit.
- Priority schemes, quotas, automatic preemption between agents
- Authentication or authorization as a security boundary (claims are
  cooperative coordination, not access control)
- Multi-tenancy across unrelated projects (see "Project Field" below)

---

## Design

### Locking Model: Hybrid Implicit + Explicit

**Explicit claims** handle extended, multi-call workflows:

```
labctl claim sbc=<name> duration=<30m|1h|4h> reason="<text>" [name=<agent-name>]
```

While claimed, any mutating operation against that SBC by a
non-claimant returns a structured error. Read-only operations
(`status`, `list`, health checks) pass through regardless — they
don't mutate state.

**Implicit short locks** handle single-shot operations:

Every mutating tool call acquires a brief (≤30 second) implicit lock
on the target SBC if no explicit claim is held. Prevents two rapid
`power_cycle` calls from interleaving, without requiring explicit
claim/release ceremony for one-off operations.

If an explicit claim is held by the caller, implicit locks are a
no-op — the claim already provides exclusion.

### Locking Granularity

**One claim per SBC.** All operations affecting an SBC (serial, SDWire,
power, network reassignment, health checks that toggle state) are
gated by the single SBC claim. Sub-resource locking (claim only
serial, only SDWire) is explicitly out of scope — it invites cases
where two agents each think they're cooperating while one power-cycles
mid-flash.

### Identity Model

Each claim records three identity fields:

1. **Agent name** (required, agent-declared): A human-readable label
   like `"jetson-gpu-agent"` or `"pi5-smp-dispatch"`. Chosen by the
   agent; surfaces in `labctl status` output.
2. **Session ID** (required, auto-derived): A fingerprint of the MCP
   connection. For stdio transport, use the PID + connection start
   timestamp. For HTTP, use the session cookie or a generated token.
   Distinguishes two instances of the same-named agent and enables
   dead-session detection.
3. **Optional context** (agent-provided metadata): git branch, worktree
   path, ticket/issue reference, user email. Not used for identity
   decisions — informational only.

Agents that fail to declare a name get a generated one
(`unnamed-<short-session-hash>`). Functional but ugly in status
output — incentive to self-declare.

### Claim Lifecycle

```
claimed → (renewal) → claimed → (release) → free
                                ↑
                                │
                         (expiry)
                                │
                                ↓
                         expired → free
```

**Duration:** specified at claim time. Bounded minimum (e.g., 1 minute)
and maximum (e.g., 4 hours) enforced by config. Typical demo-workflow
durations: 15m, 30m, 1h.

**Renewal:** any tool call by the claimant against the claimed SBC
acts as an implicit heartbeat — extends `last_activity` timestamp.
Explicit `labctl renew` allows extending duration beyond the original
window (up to max duration per renewal).

**Expiry:** if `(now - last_activity) > (duration + grace_period)`,
the claim is considered expired and released. Grace period: ~60s.

**Release:** explicit `labctl release` by the claimant (preferred) or
`labctl force-release` by an operator.

**Dead session detection:** if the MCP session associated with a claim
closes (client disconnects from stdio or HTTP session times out),
the claim enters a "stale" state and is released after grace period
expiry unless the agent reconnects and re-asserts.

### Operation Gating Matrix

| Operation | Gated by claim? | Rationale |
|-----------|-----------------|-----------|
| `list_sbcs`, `get_sbc_details` | No | Read-only metadata |
| `health_check` (ping only) | No | Non-intrusive observation |
| `serial_capture` (read stream) | No | Shared ser2net already supports this |
| `serial_send` | **Yes** | Writes to TTY; collisions corrupt commands |
| `power_on`, `power_off`, `power_cycle` | **Yes** | Disrupts whatever's running |
| `sdwire_to_host`, `sdwire_to_dut` | **Yes** | Physical reconnection; corrupts active flash |
| `sdwire_update` | **Yes** | Overwrites SD card content |
| `boot_test` | **Yes** | Multi-operation sequence; needs exclusive access throughout |
| `add_sbc`, `remove_sbc`, `update_sbc` | No | Registry mutation, not device control — can still collide if two agents target the same SBC, but these are low-frequency |
| `set_network_address`, `assign_*` | No | Configuration, not device state |

Gated operations that find an active non-caller claim return a
structured error (see "Error Format").

### User Override

Operators bypass claims via `--force` or a dedicated `labctl force-release`
command. Forced operations:

- Log the override event prominently
- Do not wait for the claimant's next call to fail — the claim is
  immediately released
- Include an operator-provided reason that surfaces in the audit log

### Request-Release Mechanism

A polite nudge without forced eviction:

```
labctl request-release sbc=<name> reason="<text>"
```

Records a request note on the active claim. The claimant sees the
request on their next operation against that SBC (surfaced in the
tool response as an advisory field). The claimant decides whether to
release early. No automatic action.

---

## Data Model

### New Table: `claims`

```sql
CREATE TABLE claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sbc_id          INTEGER NOT NULL REFERENCES sbcs(id) ON DELETE CASCADE,
    agent_name      TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    context_json    TEXT,              -- optional agent-provided metadata
    acquired_at     TIMESTAMP NOT NULL,
    expires_at      TIMESTAMP NOT NULL,
    last_activity   TIMESTAMP NOT NULL,
    renewal_count   INTEGER   NOT NULL DEFAULT 0,
    released_at     TIMESTAMP,         -- NULL while active
    release_reason  TEXT,              -- "released", "expired", "force-released", "session-lost"
    released_by     TEXT               -- agent name, "operator", or "system"
);

-- At most one active (unreleased) claim per SBC
CREATE UNIQUE INDEX idx_claims_active_sbc
    ON claims(sbc_id) WHERE released_at IS NULL;

-- Lookup acceleration
CREATE INDEX idx_claims_session ON claims(session_id) WHERE released_at IS NULL;
CREATE INDEX idx_claims_agent ON claims(agent_name) WHERE released_at IS NULL;
```

Claims are never deleted — released claims remain for audit. A retention
policy may prune released claims older than N days.

### New Table: `claim_requests`

```sql
CREATE TABLE claim_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id       INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    requested_by   TEXT    NOT NULL,   -- agent name or "operator"
    reason         TEXT    NOT NULL,
    requested_at   TIMESTAMP NOT NULL,
    acknowledged   BOOLEAN   NOT NULL DEFAULT 0
);
```

Release requests are recorded here. When the claimant sees them (via
tool response enrichment or `labctl claims show`), they can
acknowledge or act on them.

### Dataclass: `Claim`

```python
@dataclass
class Claim:
    id: Optional[int] = None
    sbc_id: int = 0
    agent_name: str = ""
    session_id: str = ""
    reason: str = ""
    context: Optional[dict] = None
    acquired_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    renewal_count: int = 0
    released_at: Optional[datetime] = None
    release_reason: Optional[str] = None
    released_by: Optional[str] = None
    pending_requests: list[ClaimRequest] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        if self.released_at is not None:
            return False
        if self.expires_at is not None and datetime.now() > self.expires_at:
            return False
        return True

    @property
    def time_remaining(self) -> Optional[timedelta]:
        if not self.is_active or self.expires_at is None:
            return None
        return self.expires_at - datetime.now()
```

Location: `src/labctl/core/models.py` alongside existing dataclasses.

### Configuration

Add to `config/labctl.yaml.example`:

```yaml
claims:
  enabled: true                  # master switch
  default_duration_minutes: 30
  max_duration_minutes: 240      # 4 hours
  min_duration_minutes: 1
  grace_period_seconds: 60       # after expires_at before actual release
  implicit_lock_seconds: 30      # short-lived per-call locks
  auto_prune_released_after_days: 30
  require_agent_name: false      # if true, reject unnamed agents
```

---

## API Surface

### CLI Commands

```
labctl claim <sbc> --duration 30m --reason "GPU bringup testing" [--name <agent>]
labctl release <sbc>
labctl renew <sbc> [--duration 30m]
labctl claims                                # list all active claims
labctl claims show <sbc>                     # details + request notes
labctl claims history <sbc> [--last 10]      # past claims on this SBC
labctl force-release <sbc> --reason "<text>" # operator override
labctl request-release <sbc> --reason "<text>"
```

Integration with existing `labctl status`:

```
$ labctl status
SBC               Power  Status   Claim
pi-5-1            OFF    ONLINE   —
pi-5-2            ON     ONLINE   jetson-gpu-agent (expires 14:32, renewed 4x)
                                    reason: FWSEC bringup testing
jetson-nano-1     ON     ONLINE   —
jetson-nano-2     ON     ERROR    pi5-dispatch-agent (expires 13:55 ⚠)
                                    reason: bench stealing reliability
                                    ⚠ pending release request from other-agent
```

### MCP Tools

New tools exposed via `mcp_server.py`:

```python
@mcp.tool()
def claim_sbc(
    sbc_name: str,
    duration_minutes: int = 30,
    reason: str = "",
    agent_name: Optional[str] = None,
    context: Optional[dict] = None,
) -> str:
    """Claim exclusive access to an SBC for a declared duration.

    Args:
        sbc_name: Name of the SBC to claim
        duration_minutes: Requested duration (bounded by config min/max)
        reason: Human-readable reason; required for audit
        agent_name: Self-declared agent identifier; falls back to
            a generated name if omitted
        context: Optional metadata (git branch, ticket, etc.)

    Returns:
        JSON with claim details on success, or structured conflict
        error if already claimed by another agent.
    """

@mcp.tool()
def release_sbc(sbc_name: str) -> str:
    """Release a claim held by the calling session.

    Only the claim holder can release via this tool. Operators use
    force_release_sbc.
    """

@mcp.tool()
def renew_sbc_claim(sbc_name: str, duration_minutes: Optional[int] = None) -> str:
    """Extend an active claim. Bounded by config max_duration."""

@mcp.tool()
def list_claims() -> str:
    """List all active claims across the lab."""

@mcp.tool()
def get_claim(sbc_name: str) -> str:
    """Get the current active claim on an SBC, including pending
    release requests."""

@mcp.tool()
def request_sbc_release(sbc_name: str, reason: str) -> str:
    """Politely ask the current claimant to release an SBC.
    Non-binding — claimant decides whether to act."""

@mcp.tool()
def force_release_sbc(sbc_name: str, reason: str) -> str:
    """Operator override — forcibly release an active claim. Logged
    prominently in the audit trail. Should only be used when normal
    release is blocked (dead agent, emergency)."""
```

Existing mutating tools gain implicit claim checks. Signature does
not change, but return value adds:

- On success with a claim held: unchanged behavior
- On failure due to other-agent claim: structured error response

### MCP Resources

```python
@mcp.resource("lab://claims")
def list_claims_resource() -> str:
    """All active claims with their metadata."""

@mcp.resource("lab://claims/{sbc_name}")
def get_claim_resource(sbc_name: str) -> str:
    """Current claim on a specific SBC."""

@mcp.resource("lab://claims/history/{sbc_name}")
def get_claim_history_resource(sbc_name: str) -> str:
    """Historical claims (released) for an SBC."""
```

---

## Error Format

Conflict responses use a consistent structure so agents can reason
about them programmatically:

```json
{
    "error": "sbc_claimed",
    "sbc_name": "jetson-nano-2",
    "message": "SBC 'jetson-nano-2' is claimed by 'jetson-gpu-agent' until 2026-04-15T14:32:00Z",
    "claim": {
        "agent_name": "jetson-gpu-agent",
        "reason": "FWSEC bringup testing",
        "acquired_at": "2026-04-15T14:02:00Z",
        "expires_at": "2026-04-15T14:32:00Z",
        "time_remaining_seconds": 1245,
        "renewal_count": 4
    },
    "hints": [
        "Wait for claim to expire or be released",
        "Request early release with request_sbc_release",
        "Operator override with force_release_sbc (requires reason)"
    ]
}
```

Other error codes:

- `claim_not_found` — release/renew on an SBC with no active claim
- `not_claimant` — release/renew attempted by non-owner
- `duration_out_of_bounds` — requested duration exceeds config limits
- `unknown_sbc` — SBC name doesn't exist in registry
- `claim_expired` — renewal attempted on a claim that has already expired

---

## Implementation Phases

### Phase A: Core claim tracking (MVP)

1. Database schema migration — add `claims` and `claim_requests` tables
2. `Claim` dataclass in `core/models.py`
3. Claim operations in `core/manager.py`:
   `claim_sbc()`, `release_claim()`, `renew_claim()`,
   `get_active_claim()`, `list_active_claims()`,
   `force_release_claim()`, `expire_stale_claims()`
4. CLI commands: `claim`, `release`, `renew`, `claims`, `force-release`
5. Update `labctl status` to surface claims
6. Unit tests covering: acquisition, conflict, expiry, force-release,
   session-scoped ownership, concurrent acquisition race

### Phase B: MCP integration

1. New MCP tools (`claim_sbc`, `release_sbc`, etc.)
2. New MCP resources (`lab://claims`, etc.)
3. Session ID derivation (stdio PID+timestamp, HTTP cookie)
4. Implicit claim checks on mutating tools
5. Structured conflict error responses
6. Integration tests against a running MCP server

### Phase C: Expiry and dead-session handling

1. Background expiry worker (periodic sweep of stale claims)
2. Session liveness tracking — detect MCP disconnects
3. Grace period logic
4. Logging of auto-release events

### Phase D: Operator tooling

1. `labctl claims history` command
2. Audit log with filters (by SBC, by agent, by outcome)
3. Web dashboard integration — claim indicator per SBC
4. Claim request notifications surfaced in tool responses

### Phase E: Polish

1. Config validation
2. Retention / auto-prune of released claims
3. Metrics (claim acquire/release counts, average durations)
4. Documentation updates (MCP_SERVER.md, README.md, AGENT_RULES.md)
5. Example agent onboarding flow

---

## Migration & Backward Compatibility

- Feature is **opt-in via config** (`claims.enabled: true`). When
  disabled, behavior matches current labctl exactly.
- When enabled but no claim is held on a given SBC, implicit
  short-locks provide safety without requiring agents to adopt the
  claim API immediately.
- Existing tools do not change signatures — only error responses are
  extended with new conflict codes.
- Database migration is additive (new tables only) — no existing
  schema affected.

## Relationship to `project` Field

The existing `project` field on `SBC` remains useful as a **tag**, not
a scoping dimension:

- Filter `labctl status --project=slm-os`
- Future multi-project deployments (if labctl serves distinct projects)
- Audit log grouping

A project can have many agents. Agents within a project claim
individual SBCs. Project is orthogonal to claims.

## Security Considerations

Claims are **cooperative coordination**, not security. An agent that
chooses to bypass the claim API can still operate on hardware. The
feature prevents accidental collisions, not malicious override.

A future hardening pass could:
- Require signed claim tokens
- Enforce authentication before tool invocation
- Rate-limit force-release

Out of scope for this spec. If the current labctl deployment runs on
a trusted workstation with shared operator trust, cooperative claims
are sufficient.

## Open Questions

1. **Session ID derivation for stdio MCP:** PID + start timestamp is
   simple but PIDs recycle. Acceptable for short-lived sessions?
   Alternative: require explicit session registration on first tool
   call.
2. **Claim visibility to the web dashboard:** presumably yes, but
   needs coordinated UI design — out of scope for Phase A.
3. **Interaction with `/deploy-and-test` skill:** should the skill
   auto-claim for the duration of its run? Probably yes. Design
   decision for skill update alongside Phase B.
4. **HTTP MCP session tracking:** FastMCP HTTP transport session
   lifecycle needs inspection — where does the session token live,
   how is disconnect detected?

---

## Acceptance Criteria

- Agent can `claim`, `renew`, `release` an SBC via CLI and MCP
- Non-claimant mutating operations against a claimed SBC return the
  structured conflict error
- Claims auto-expire after duration + grace period
- Operator can `force-release` any active claim
- Claim state survives labctl process restarts (stored in SQLite)
- Dead MCP sessions release their claims within grace period
- Existing workflows (CLI + web + MCP) work unchanged when claims
  feature is disabled
- Existing workflows work unchanged when claims feature is enabled
  but no agents are using it

---

## References

- Origin discussion: SLM-OS multi-agent coordination, April 2026
- Related: existing `project` field (keep, but separate concern)
- Related: `/deploy-and-test` skill (Phase B integration)

*Created: 15 April 2026*
