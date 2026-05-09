# SPEC: USB Relay Actuators + Per-Target Bindings

Specification for first-class support of USB-controlled relays and
similar single-bit actuators as **provisioned lab resources** that can
be **assigned to named per-target actuations**. The relay device itself
is use-agnostic; specific behaviors (USB Force Recovery on a Jetson,
boot-select strap on a board, simulated GPIO input, etc.) are layered
on top via bindings.

**Status:** Proposed
**Source request:** Lab-day request, 2026-05-09
**Target release:** v0.x
**Motivating case:** Jetson Orin Nano USB Force Recovery (J14 9–10) without a physical jumper

---

## Goals

- Provision USB relays (and similar single-bit actuators) as named lab
  resources alongside SBCs, serial ports, and SDWire devices.
- Bind actuator channels to (target, purpose) pairs so the user-facing
  surface is semantic (`enter_recovery jetson-nano-2`), not
  device-specific (`set relay 0 closed`).
- Make `power_on` / `power_cycle` consult bindings automatically so a
  recovery-mode strap "just works" across power transitions.
- Default to a known-safe state on daemon start without dropping
  intentionally-asserted straps mid-flash.
- Stay extensible: adding a driver, a purpose, or a target type should
  not require schema migrations.

## Non-goals (v1)

- Universal relay-driver abstraction. v1 targets one driver
  (`lcus1_serial`) plus the binding/composition layer; other drivers
  ship in follow-ups.
- Vendor-specific composite operations (e.g. `reflash_secure`). Out of
  scope until the v1 surface is proven on the Jetson case.
- Multi-channel coordinated transitions (boot-select + reset together
  with timing). Stretch.
- Web UI for actuator state. Stretch.

---

## Naming

Avoid baking "force reset" into the public API: it conflates distinct
mechanisms (Tegra USB Force Recovery, ARM nSRST/SRST, power-good
cycling, watchdog assert) and locks us out of boards that don't have a
single "reset" line. Use:

- **actuator** — the physical resource (a relay channel, later a GPIO
  or logic-level driver).
- **binding** — a named association between an actuator channel and a
  (target, purpose) pair.
- **purpose** — what the binding means semantically. Predefined set,
  extensible by config.

User-facing verbs are per-purpose, not per-relay:
`enter_recovery jetson-nano-2`, `press_power_button rpi-5-1`, etc.

---

## Data model

```
Actuator (lab-level)
├── id: stable name, e.g. "usbrelay-rack1-a"
├── kind: relay | gpio | …
├── driver: lcus1_serial | numato_acm | hid_generic | sysfs_gpio | …
├── transport: { device_path, vid, pid, serial_no }
├── channels: [
│     { index, label?, default_state: open|closed,
│       last_state, last_changed_at, cycle_count }
│   ]
├── claimed_by: <agent_name>?
└── health: { reachable, last_probe_at, last_probe_result }

Binding (use-level)
├── id: "<target_type>:<target_id>:<purpose>"
├── actuator_id, channel_index
├── target_type: sbc | …
├── target_id: SBC name (or other lab-resource name)
├── purpose: predefined string (see below)
├── shape:
│     { mode: latch | momentary,
│       active: closed | open,
│       momentary_pulse_ms?: int,
│       sample_phase: pre_power | post_power | none }
├── desired_state: asserted | released | following_power
└── notes: free text (wiring photo URL, polarity, etc.)
```

### `desired_state` (persistent intent)

`last_state` records what we *last drove*; `desired_state` records what
the operator *wants*. The two diverge when:

- Daemon restarts. `last_state` is lost; `desired_state` persists.
- Hardware fails. `last_state` may be stale; `desired_state` is still
  the operator's instruction.
- Power cycles consult `desired_state` to decide whether to re-assert
  a `pre_power` strap before applying power.

`assert sbc <purpose>` sets `desired_state = asserted`. `release` sets
`desired_state = released`. `power_on` and `power_cycle` read
`desired_state` for any binding with `sample_phase = pre_power` and
drive the actuator to the appropriate physical state before applying
power.

### Channel count: hint vs. enumeration

`--channels N` at provisioning time is a *hint*, used only by drivers
that cannot enumerate (e.g. LCUS-1, which has no protocol query for
channel count). For drivers that can enumerate (Numato), the driver
populates `channel_count` on first probe and rejects a mismatching
hint with an actionable error:

```
labctl actuator add --driver numato_acm --channels 4 ...
Error: driver enumerated 8 channels on /dev/ttyACM0; --channels 4 disagrees.
       Pass --channels 8, or omit it (driver will fill in).
```

`--channels` is required only for non-enumerable drivers, where it's a
declaration the driver trusts.

---

## Purposes (initial enum)

| purpose          | shape default      | notes                                                                             |
|------------------|--------------------|-----------------------------------------------------------------------------------|
| `recovery_mode`  | latch / pre-power  | Tegra USB Force Recovery (J14 9-10), Pi BCM bootmode straps, etc.                 |
| `boot_select`    | latch / pre-power  | A/B slot strap, BOOT0 line on STM32, etc.                                         |
| `power_button`   | momentary 200 ms   | Press/hold the SBC's soft power switch.                                           |
| `reset_button`   | momentary 100 ms   | Asserts nRESET; not all boards expose one cleanly.                                |
| `usb_data_break` | latch / any        | Cut data lines of a downstream USB device for force-renumerate.                   |
| `usb_power_break`| latch / any        | Cut Vbus to a downstream USB device.                                              |
| `dut_input`      | latch or momentary | Generic "close two pins together" — for simulating button presses, GPIOs, etc.    |

Adding a purpose is a config + CLI label addition, not a schema
migration.

### Wiring polarity validation

At binding creation, refuse a config where `default_state == active`.
That would mean "the device boots in this state every cold boot,"
which for a `recovery_mode` binding means "always boot in recovery" —
almost certainly a wiring mistake. The error:

```
Error: 'recovery_mode' binding has default_state=closed and active=closed.
       This means the device would always boot in recovery.
       Verify wiring, or pass --shape latch:open-active.
```

---

## Driver layer

```python
class RelayDriver:
    def open(self, transport) -> None: ...
    def close(self) -> None: ...
    def set_channel(self, index: int, state: bool) -> WriteOutcome: ...
    def get_channel(self, index: int) -> bool | None: ...   # None if not queryable
    def channel_count(self) -> int | None: ...              # None if not enumerable
    def probe(self) -> ProbeResult: ...                     # health check, side-effect-free
```

### `WriteOutcome` (replaces `None` return)

Every USB write returns structured status so composite operations can
make safe decisions:

```python
class WriteOutcome(Enum):
    OK = "ok"                 # write completed and was acknowledged
    WRITE_FAILED = "write_failed"   # device rejected or returned error
    DEVICE_GONE = "device_gone"     # device disappeared (USB disconnect, etc.)
```

Composite operations abort on anything other than `OK`. See
"Failure-mode design" below.

### `probe()` is side-effect-free

Probes never toggle channels. For drivers with state queries (Numato),
probe reads back current state. For write-only drivers (LCUS-1), probe
opens the serial port and verifies the descriptor, but does *not* send
a state-changing command. Toggling a channel during `probe` could drop
a strap mid-flash; the cure mustn't be worse than the disease.

### Initial drivers (v1 ships only `lcus1_serial`)

- **`lcus1_serial`** — CH340-based LCUS-1 / generic clones. 4-byte
  ASCII command at 9600 baud; one channel; not queryable (track
  software state). **v1 driver.**
- **`numato_acm`** — Numato 1/4/8/16-channel USB relay. CDC-ACM,
  ASCII protocol, queryable. *Deferred to v2.*
- **`hid_generic`** — for the HID-style "drive-free" Chinese boards.
  *Deferred.*
- **`sysfs_gpio`** — host GPIO via sysfs. *Deferred.*

### USB device identity (LCUS-1 caveat)

LCUS-1 boards almost universally use CH340 chips **without serial
numbers**. `/dev/serial/by-id/usb-CH340...` collides across identical
relays. v1 requires either:

1. Serial-number-bearing relays (use a Numato or a CH340 variant with
   programmed serial), or
2. A udev rule pinning the relay to a stable name like
   `/dev/lab/relay-rack1-a` based on USB topology (port path),
   following the same convention as the existing USB-serial adapter
   handling.

Document option 2 as the standard for LCUS-1.

---

## CLI / MCP surface

### Provisioning (admin)

```
labctl actuator add usbrelay-rack1-a \
    --driver lcus1_serial --device /dev/lab/relay-rack1-a \
    --channels 1
labctl actuator list
labctl actuator probe usbrelay-rack1-a
labctl actuator set usbrelay-rack1-a 0 closed       # raw, bypasses bindings
labctl actuator remove usbrelay-rack1-a
```

MCP equivalents: `actuator_add`, `actuator_list`, `actuator_probe`,
`actuator_set`, `actuator_remove`. **MCP `actuator_*` tools require
admin scope** so agents going through MCP can't bypass bindings.

### Binding (admin)

```
labctl bind jetson-nano-2 recovery_mode \
    --actuator usbrelay-rack1-a --channel 0 \
    --shape latch:closed-active --sample-phase pre-power
labctl bindings list [--target jetson-nano-2]
labctl unbind jetson-nano-2 recovery_mode
```

MCP: `bind`, `unbind`, `bindings_list`.

### Operation (user agent)

```
labctl actuate jetson-nano-2 recovery_mode      # = assert; sets desired=asserted
labctl release jetson-nano-2 recovery_mode      # sets desired=released
labctl press   jetson-nano-2 power_button       # momentary pulse
labctl status  jetson-nano-2 recovery_mode      # current state + desired
```

MCP: `actuate`, `release`, `press`, `actuation_status`.

> The verb `actuate` (not `assert`) avoids the Python keyword and JS
> reserved word, which would cause friction in MCP tool-name dispatch.

### Verb / shape congruence (hard error on mismatch)

`press` requires a momentary binding. `actuate` / `release` require a
latch binding. Calling the wrong verb against the binding's declared
shape is an error, not a silent default:

```
$ labctl press jetson-nano-2 recovery_mode
Error: 'recovery_mode' is bound as latch (not momentary).
       Use 'actuate' / 'release' for sustained state, or rebind with
       --shape momentary:200ms if you want press semantics.
```

Rationale: silent fallback (e.g. "treat press-on-latch as a 200 ms
pulse") leaves the operator with a recovery_mode strap held when they
thought they'd released it. The 200 ms momentary default applies only
when a binding is *created* with `--shape momentary` and no explicit
duration.

### Composite, power-aware operations

The operations that justify the relay install:

```
labctl enter_recovery jetson-nano-2
    # 0. probe all touched actuators (read-only)        ← pre-flight
    # 1. power_off
    # 2. sleep(power.min_off_ms)
    # 3. actuate recovery_mode  → desired=asserted
    # 4. power_on
    # 5. wait_for usb_apx_device                        (optional)
labctl exit_recovery jetson-nano-2
    # 1. power_off
    # 2. sleep(power.min_off_ms)
    # 3. release recovery_mode  → desired=released
    # 4. power_on
```

MCP: `enter_recovery`, `exit_recovery`. `reflash_secure` is **not** in
v1 — too opinionated about BSP layout.

### Power-cycle integration

`shape.sample_phase` controls when bindings participate in
power flows:

- `pre_power` — actuator must be in target state **before** power is
  applied. `power_on` / `power_cycle` consult bindings' `desired_state`
  and drive the actuator accordingly before applying power.
- `post_power` — actuator is driven after the device is up. *Deferred.*
- `none` — no automatic coupling; user drives explicitly.

A target with `pre_power` bindings whose `desired_state == asserted`
stays asserted across power cycles until `release`d. This is what
lets `power_cycle` "just do the right thing" without a separate
`--in-recovery` flag.

---

## Concurrency, safety, and failure modes

### Single-writer per channel

`actuate` / `release` / `press` require holding a per-channel lock.
Locks are non-blocking with a clear "channel busy" error rather than
queueing — agents should retry or back off explicitly.

### SBC claim composition (with non-blocking actuator claims)

Claiming an SBC implicitly *attempts* to acquire any actuator channels
its bindings reference. If any channel is busy, the SBC claim
**fails immediately** with a "shared actuator busy on <channel>"
message instead of blocking. This avoids the lock-ordering deadlock
where two SBCs share a multi-channel relay.

Releasing the SBC releases all linked channels.

### Default-safe drive on daemon start (gated by `desired_state`)

On daemon start, every channel is evaluated:

| binding state                                  | action on daemon start                |
|------------------------------------------------|---------------------------------------|
| no binding for channel                         | drive to `default_state`              |
| binding with `desired_state = released`        | drive to `default_state`              |
| binding with `desired_state = asserted`        | **leave alone**, log warning event   |
| binding with `desired_state = following_power` | leave alone (driven on next power_on) |

The `desired_state == asserted` warning is the operator's signal that
something was mid-operation when the daemon went down. They can
re-run `enter_recovery` (idempotent) or `release` to clean up. We do
**not** snap a held strap to default — that would drop a flash in
progress.

### USB disconnect mid-composite

The hazard: USB relay drops between `actuate recovery_mode` and
`power_on`. Without guards, the host applies power, the device boots
normally (no strap), the operator launches a flash, and it fails
confusingly. Three guard rails:

1. **Pre-flight probe.** `enter_recovery` runs `probe` on every
   actuator it'll touch *before* `power_off`. Unreachable actuator =
   abort before any power transition.
2. **Write-or-abort transitions.** Every USB write returns
   `WriteOutcome`. Anything other than `OK` aborts the composite,
   leaves power off, and emits an "actuator state uncertain" event.
   The composite is structured so aborting between any two steps
   leaves a *safe* state: power off, strap state irrelevant.
3. **`desired_state` survives the disconnect.** If the operator's
   intent was `desired=asserted`, daemon restart preserves it (rule
   above), and the operator's next `enter_recovery` is idempotent.

Net behavior: a USB disconnect mid-sequence means power stays off, a
diagnostic event fires, and the operator is told to reseat the relay
and retry. No silent half-recoveries.

### Health probes (read-only)

`actuator probe` is side-effect-free. For LCUS-1 (write-only),
probe = "open the serial port, verify the device descriptor, do not
send a state-changing command." For Numato, probe = "read current
channel state via `relay read`." Surfaces in the actuator's `health`
field and reported by `run_health_check` alongside SBC checks.

### Wear telemetry (deferred)

Track `cycle_count` per channel in v1, but no threshold-warning
logic. Add it when we have a real-world cycle count to base a default
on; cheap mechanical relays vary widely.

---

## Per-device timing hints

The recently-shipped `power_cycle_delay_seconds` column on `sbcs`
(schema v7) is the source of truth for SBC-level power timing.

For v1, extend this with `min_off_ms_for_recovery` if needed (likely
named `min_off_ms` since `power_cycle_delay_seconds` already covers
the general case). DB-only — do **not** introduce a parallel YAML
config knob; the existing CLI surface (`labctl edit --cycle-delay`)
is the configuration path.

If the spec lands without an immediate use for a separate
`min_off_ms_for_recovery`, reuse `power_cycle_delay_seconds` for now
and add a dedicated column when a target needs different
power-cycle timings for normal vs. recovery flows.

---

## Migration

- Existing `power_on` / `power_off` / `power_cycle` keep working
  unchanged. They grow a "consult bindings with `pre_power` shape"
  step that is a no-op when no bindings exist on the SBC.
- Existing `claim_sbc` / `release_sbc` grow the
  "auto-acquire/release linked actuator channels" behavior with
  non-blocking semantics.
- New CLI/MCP verbs are additive; nothing in today's surface is
  renamed or removed.
- Schema migration v7 → v8 adds `actuators`, `actuator_channels`, and
  `bindings` tables. SBCs unaffected.

---

## v1 scope cut

The narrowest set that delivers the Jetson case end-to-end:

| ✅ in v1 | ⏸️ deferred |
|----------|---------------|
| Data model (Actuator + Binding + Purpose + `desired_state`) | Wear-telemetry threshold warnings |
| `lcus1_serial` driver only | `numato_acm`, `hid_generic`, `sysfs_gpio` drivers |
| CLI: `actuator add/list/probe/remove/set` | Web UI for actuator state |
| CLI: `bind/unbind/bindings list` | `reflash_secure` |
| CLI: `actuate/release/press/status` | `post_power` sample_phase |
| `enter_recovery` / `exit_recovery` composites | Hardware fault detection (APX-not-seen heuristics) |
| `pre_power` sample_phase | `bind test` validation harness |
| SBC claim auto-acquires linked channels (non-blocking) | Multi-channel coordinated bindings |
| Daemon-start default-safe drive gated by `desired_state` | Per-channel cycle-count thresholds |
| Pre-flight probe + WriteOutcome abort semantics | |
| Verb/shape congruence enforcement | |
| Wiring-polarity validation at bind time | |

That's ~60% of the original spec, all of the Jetson value, none of the
speculative scaffolding. Each deferred item has a clear trigger
condition for when it should ship.

---

## Open questions

- **Power timing column name.** Reuse `power_cycle_delay_seconds` or
  add a parallel `min_off_ms_for_recovery`? Default to reuse until a
  target shows the two diverging.
- **Binding ID format.** `<target_type>:<target_id>:<purpose>` is
  human-readable but constrains target IDs (no colons in SBC names).
  Acceptable today (no SBC has a colon), revisit if it bites.
- **Multi-target actuator channels.** Spec assumes 1 channel ↔ 1
  binding. A channel bound to two targets is currently unrepresentable
  — fine for v1, may need revisiting if rack power-button arrays land.
