# Operation classification

Which labctl operations change what, so you know what is safe to run while
someone else is using the lab (issue #7, decision D012). There is no
enforced read-only tier yet: this is documentation. Access is governed by
`labctl` group membership, claims, and (for MCP) tool annotations plus the
two-step confirmation for destructive tools.

## Classes

| Class | Meaning |
|---|---|
| read | No persistent writes (apart from a first-run DB schema setup). The MCP `sdwire_ls`/`sdwire_cat`/`sdwire_info` tools are read even though they may switch the SD card to the host and back; their notes say when |
| db-write | Writes only the labctl database (SBCs, claims, bindings, audit rows) |
| shared-resource | Holds a lab resource someone else may be using (a ser2net console session, a relay's serial port) for as long as it runs, without changing persistent state |
| system-write | Writes system files or restarts system services |
| hardware | Changes physical state, but recoverably (power on, SD card to the board) |
| destructive | Can destroy data, disrupt a running board, or override another user (power off/cycle, flashing, SD file changes, typing into a console, deleting records, force-releasing a claim) |

Every command that touches the database also does a one-time schema setup
the first time it runs on a new or old database (WAL pragma, migrations).
"read" ignores that.

## MCP tools

"Confirm: yes" means the tool is annotated destructive and, by default,
requires the two-step confirmation: the first call returns a plan and a
`confirm_token` and does nothing; a second call with the same arguments plus
that token acts. Tokens are single-use, expire after 120 s, and are bound to
the tool and its exact arguments. Configure with `mcp.confirm_destructive`
(default `true`) and `mcp.confirm_exempt` (tool names, e.g. `serial_send`).

| Tool | Class | Confirm | Notes |
|---|---|---|---|
| `actuate` | destructive | yes |  |
| `actuation_status` | read |  |  |
| `actuator_add` | db-write |  | Off by default: refused unless `mcp.allow_admin_actuator_ops: true` |
| `actuator_list` | read |  |  |
| `actuator_probe` | shared-resource |  | Briefly opens the relay's serial port; also records last_probe_* |
| `actuator_remove` | destructive | yes | Off by default: refused unless `mcp.allow_admin_actuator_ops: true` |
| `actuator_set` | destructive | yes | Off by default: refused unless `mcp.allow_admin_actuator_ops: true` |
| `add_sbc` | db-write |  |  |
| `add_serial_device` | db-write |  |  |
| `assign_power_plug` | db-write |  |  |
| `assign_serial_port` | db-write |  |  |
| `bind` | db-write |  |  |
| `bindings_list` | read |  |  |
| `boot_test` | destructive | yes |  |
| `claim_sbc` | db-write |  |  |
| `delete_image` | destructive | yes | Deletes a file in `mcp.upload_dir`; only its uploader or an operator |
| `enter_recovery` | destructive | yes |  |
| `exit_recovery` | destructive | yes |  |
| `flash_image` | destructive | yes |  |
| `force_release_sbc` | destructive | yes |  |
| `get_claim` | read |  |  |
| `list_claims` | read |  |  |
| `list_images` | read |  | Uploading is not a tool: `PUT /images/<name>` on the HTTP port writes `mcp.upload_dir` (system-write; its uploader or an operator may replace an image) |
| `power_cycle` | destructive | yes | Sets pre_power relays like `power_on`, after power-off; `delay` 0-86400 s; default and minimum the largest stored cycle delay among the boards on the outlet (3.0 for one without) |
| `power_off` | destructive | yes |  |
| `power_on` | hardware |  | First sets the relays bound with `sample_phase: pre_power` (e.g. a strap) on this board and every board sharing its plug outlet; all are checked first, and power is not switched if one can't be set |
| `press` | destructive | yes |  |
| `release` | destructive | yes |  |
| `release_sbc` | db-write |  |  |
| `remove_network_address` | destructive | yes |  |
| `remove_power_plug` | destructive | yes |  |
| `remove_sbc` | destructive | yes |  |
| `remove_serial_device` | destructive | yes |  |
| `remove_serial_port` | destructive | yes |  |
| `renew_sbc_claim` | db-write |  |  |
| `request_sbc_release` | db-write |  |  |
| `run_health_check` | shared-resource |  | Pings, briefly opens the ser2net port, queries the plug; writes nothing |
| `sdwire_add` | db-write |  |  |
| `sdwire_assign` | db-write |  |  |
| `sdwire_cat` | read |  | As sdwire_ls |
| `sdwire_discover` | read |  |  |
| `sdwire_info` | read |  | As sdwire_ls |
| `sdwire_ls` | read |  | Reads in place if the card is already on the host (left there); else switches to the host and back to the board only when the plug reports OFF, and refuses otherwise (no plug, unreadable or unknown state) |
| `sdwire_remove` | destructive | yes |  |
| `sdwire_to_dut` | hardware |  |  |
| `sdwire_to_host` | destructive | yes |  |
| `sdwire_unassign` | destructive | yes |  |
| `sdwire_update` | destructive | yes |  |
| `serial_capture` | shared-resource |  | Holds a ser2net session while capturing; sends nothing |
| `serial_discover` | read |  |  |
| `serial_send` | destructive | yes |  |
| `set_network_address` | db-write |  |  |
| `unbind` | destructive | yes |  |
| `update_sbc` | db-write |  |  |

## MCP resources

| Resource | Class | Notes |
|---|---|---|
| `lab://activity/recent` | read | Database only |
| `lab://activity/{sbc_name}` | read | Database only |
| `lab://claims` | read | Database only |
| `lab://claims/history/{sbc_name}` | read | Database only |
| `lab://claims/metrics` | read | Database only |
| `lab://claims/{sbc_name}` | read | Database only |
| `lab://health/{sbc_name}` | read | Queries hardware (power plug / health checks); takes the hardware lock |
| `lab://ports` | read | Database only |
| `lab://power/{sbc_name}` | read | Queries hardware (power plug / health checks); takes the hardware lock |
| `lab://sbcs` | read | Database only |
| `lab://sbcs/{sbc_name}` | read | Database only |
| `lab://sdwire-devices` | read | Database only |
| `lab://serial-devices` | read | Database only |
| `lab://status` | read | Queries hardware (power plug / health checks); takes the hardware lock |

## CLI commands

"Prompts" means the command asks for confirmation before acting (skip with
`--yes`). Every destructive command does. They ask **only on an interactive
terminal**, i.e. when stdin is a terminal. Callers without one (cron, CI,
systemd, pipes, agents' tool calls) are not asked. A shell script started
from an interactive terminal inherits it and **is** asked, so add `--yes` to
scripted destructive commands. The exceptions are `remove`, `sdwire
remove`, `serial remove`, `actuator remove` and `power-all`: without a
terminal they don't go ahead unasked, but refuse (exit 1) unless given
`--yes`.
`power cycle`, `sdwire flash` and `sdwire update` also take `--dry-run`.
`sdwire flash`/`update` (CLI and MCP) switch the board off before taking
its card and write nothing if that fails. A board with no power plug
can't be switched off or known to be off, so they refuse it unless
`--no-plug-ok` (MCP `no_plug_ok=true`): you've made sure it is off. That
flag never skips the power-off of a board that has a plug, nor excuses a
failed one. (`boot-test` needs a plug anyway, so it has no such flag.)

**Claims (D013):** commands that change a board refuse when another user
has claimed it, naming the holder: those that operate it (`power
on/off/cycle`, `power-all`, `sdwire dut/host/flash/update/ls/cat/info`,
`serial send`, `bindings actuate/release/press`,
`enter-/exit-recovery`, `boot-test`), those that change its records
(`edit`, `remove`, `port`/`plug`/`network` assign/set/remove,
`sdwire assign/unassign`, `bind`/`unbind`), `actuator set`/`remove` on
channels bound to a claimed board, `serial remove` of an adapter a claimed
board has a port on (by any path that reaches it: `/dev/lab/<name>`, its
registration, or a path resolving to the same device node), `serial udev
--install` when the new rules remove or re-point a `/dev/lab/<name>` link
a claimed board's port uses (every link, if the installed rules file can't
be read), and `import` (checked for every board before anything changes).
`edit` checks only the board itself, except `edit --cycle-delay`: a
stored delay is the minimum of every board on the plug outlet, so setting
or clearing it is also refused when a board sharing its hardware is
claimed by someone else.
As over MCP, a claim covers hardware shared with other boards: operating
a board that shares a plug outlet, SDWire, console adapter or relay
channel with a claimed board is refused, as are assigning that hardware
to another board and `claim` on a board that shares it. `power-all`
skips claimed boards. `--override-claim` acts anyway and records a
`claim_override` entry in the activity log against the claimed board
when the claim check passes: after the confirmation question (so a "no"
records nothing), but before the command's own checks and action, so a
command that then fails (no plug assigned, a device error) still leaves
the entry; its own result is logged after it. `import` checks, and
records overrides for, every board before it starts. A `--dry-run`
reports the override instead of recording it.
`remove --force` still works as an alias. The CLI's own claims (`labctl
claim`, per user and host) count as the user's; claims made over MCP or
the web don't, even by the same person. Under `sudo` the user is the one
who ran sudo (`SUDO_USER`), not root, so e.g. `sudo labctl serial udev
--install` keeps your own claims yours; the activity log names that user
too. `claims.require_claim` doesn't
apply to the CLI, and with `claims.enabled: false` nothing is checked or
logged (`remove` still refuses a board with an open claim unless
`--override-claim`). Not checked: reading a console and shell access
(`connect`, `console`, `serial capture`, `ssh`, `proxy start`), `log`, `health-check`,
`monitor`, `actuator probe`, the `claims` commands, lab-wide serial setup
(`ser2net`, `serial rename`/`repair`,`serial udev` without `--install`),
which affects every board's consoles at once, and the hardware inventory
(`add`, `serial add`, `sdwire add`/`remove`, `actuator add`). `sdwire
remove` refuses a device that is still assigned. `serial remove` also
refuses, whoever holds a claim, an adapter still linked to a port (assigned
with `--serial-device` or by a `/dev/lab/<name>` path naming a registered
adapter).
The top-level aliases `off` (`power off`), `on` (`power on`),
`rm`/`delete` (`remove`), `ls` (`list`) and `show` (`info`) behave
exactly like the commands they stand for.

| Command | Class | Prompts | Notes |
|---|---|---|---|
| `activity export` | read |  | NDJSON to stdout |
| `activity tail` | read |  |  |
| `actuator add` | db-write |  |  |
| `actuator list` | read |  |  |
| `actuator probe` | shared-resource |  | Briefly opens the relay's serial port; records last_probe_* |
| `actuator remove` | destructive | yes | Deletes the actuator and its bindings |
| `actuator set` | destructive | yes | Drives a relay channel directly, bypassing bindings |
| `add` | db-write |  |  |
| `bind` | db-write |  |  |
| `bindings actuate` | destructive | yes | Purpose is free text (power button, reset, strap...) |
| `bindings list` | read |  |  |
| `bindings press` | destructive | yes | As above |
| `bindings release` | destructive | yes | As above |
| `bindings status` | read |  |  |
| `boot-test` | destructive | yes | Repeated power cycles; optional image deploy; writes run files |
| `claim` | db-write |  |  |
| `claims expire` | db-write |  | Releases other agents' expired/dead claims; prunes history |
| `claims history` | read |  |  |
| `claims list` | read |  |  |
| `claims show` | read |  |  |
| `claims stats` | read |  |  |
| `completion` | read |  |  |
| `connect` | shared-resource |  | Interactive console; what you type reaches the board |
| `console` | shared-resource |  | As `connect` |
| `edit` | db-write |  | `--cycle-delay` also claim-checks boards sharing its hardware |
| `enter-recovery` | destructive | yes | Power off, assert strap, power on |
| `exit-recovery` | destructive | yes | Power cycle with the strap released |
| `export` | read |  | Writes a file only with `-o` |
| `force-release` | destructive | yes | Overrides another user's claim |
| `health-check` | shared-resource |  | Opens each ser2net port briefly; writes status rows only with `-u` |
| `import` | db-write |  | Skips existing SBCs; `-u` updates their fields (project, description, ...) and ports/addresses/plugs |
| `info` | read |  |  |
| `list` | read |  |  |
| `log` | shared-resource |  | Holds the console session; appends to a host file (default `./<sbc>-<ts>.log`) |
| `mcp` | server |  | Long-running; see the MCP tables |
| `monitor` | shared-resource |  | Long-running; writes status, power-cache, alert and audit rows; opens ser2net ports each cycle |
| `network remove` | destructive | yes | Deletes a record |
| `network set` | db-write |  |  |
| `plug assign` | db-write |  |  |
| `plug remove` | destructive | yes | Deletes a record |
| `port assign` | db-write |  |  |
| `port list` | read |  |  |
| `port remove` | destructive | yes | Deletes a record |
| `ports` | read |  |  |
| `power cycle` | destructive | yes | `--dry-run`; the outlet's minimum (largest stored cycle delay among its boards, 3 s for one without) is the default and a floor; sets pre_power relays as `power on` |
| `power off` | destructive | yes |  |
| `power on` | hardware |  | Sets pre_power relays first, as MCP `power_on` |
| `power status` | read |  |  |
| `power-all` | destructive | yes | Every SBC (or a project) at once; `on` sets pre_power relays as `power on` |
| `proxy list` | read |  |  |
| `proxy start` | shared-resource |  | Holds the console session; writes proxy state/logs; `--allow-write` lets clients type |
| `release` | db-write |  |  |
| `remove` | destructive | yes | Deletes the SBC and its data |
| `renew` | db-write |  |  |
| `request-release` | db-write |  |  |
| `sdwire add` | db-write |  |  |
| `sdwire assign` | db-write |  |  |
| `sdwire cat` | hardware |  | Switches the SD card to the host, then to the board; only when the plug reports the board off (see note below) |
| `sdwire discover` | read |  |  |
| `sdwire dut` | hardware |  |  |
| `sdwire flash` | destructive | yes | Overwrites the whole SD card; `--dry-run`; powers the board off first, refusing a board with no plug unless `--no-plug-ok` |
| `sdwire host` | destructive | yes | Pulls the card from the board; only when the plug reports it off (no plug or an unreadable plug refuses too); `--force` skips the power check |
| `sdwire info` | hardware |  | As `sdwire cat` |
| `sdwire list` | read |  |  |
| `sdwire ls` | hardware |  | As `sdwire cat` |
| `sdwire remove` | destructive | yes | Deletes a record |
| `sdwire unassign` | destructive | yes | Deletes an association |
| `sdwire update` | destructive | yes | Copies/renames/deletes files on the card; `--dry-run`; power-off and no-plug rule as `sdwire flash` |
| `ser2net generate` | system-write |  | Only with `--install`: saves a TCP port for any port without one, then writes `ser2net.config_file` (default `/etc/ser2net.yaml`), atomically when it can create files in that directory and otherwise in place, refusing a symlink or hard link there. Otherwise writes stdout or the `-o` file |
| `ser2net reload` | system-write |  | Restarts ser2net: drops every live console session |
| `serial add` | db-write |  |  |
| `serial capture` | shared-resource |  | Holds a ser2net session; sends nothing |
| `serial discover` | read |  | Prints only; registers nothing |
| `serial list` | read |  |  |
| `serial remove` | destructive | yes | Deletes a record |
| `serial rename` | db-write |  |  |
| `serial repair` | db-write |  | Dry run unless `--apply` |
| `serial send` | destructive | yes | Types into a running board's console (no proxy guard) |
| `serial udev` | system-write |  | Writes udev rules only with `--install`; `--reload` reloads udev, and only together with `--install` |
| `services status` | read |  |  |
| `sessions` | read |  |  |
| `ssh` | shared-resource |  | Interactive shell on the board |
| `status` | read |  | Queries plugs live; `--fast` reads cached state |
| `unbind` | destructive | yes | Deletes a binding |
| `user add` | read |  | Prints a config snippet |
| `user generate-key` | read |  |  |
| `user hash-password` | read |  |  |
| `user verify` | read |  |  |
| `web` | server |  | Long-running web UI/API |

**SD card power check:** `sdwire host`, `sdwire ls`/`cat`/`info` and
the MCP `sdwire_to_host`/`sdwire_ls`/`cat`/`info` take the card from the
board only when its plug reports it OFF: no power plug, a plug that can't
be read, or an unknown state refuse too. `--force` on all four CLI
commands (MCP `sdwire_to_host(force=true)`) skips the check; use it once
you've made sure the board is off, e.g. on a board with no plug. The read
commands (CLI and MCP) read a card that is already on the host in place
and leave it there, with no power check: the board isn't using it.

## Web UI and REST API

**Claims (D013):** a request that operates or reconfigures a board is
refused when another user has claimed it: the REST API answers `409`
with `"error": "sbc_claimed"` (the holder, their claim and hints), the web
UI shows a page naming the holder. That covers power (`POST
/api/sbcs/<name>/power`, the power buttons), editing and deleting a board
(`PUT`/`DELETE /api/sbcs/<name>`, the edit form) and its ports, network
addresses and power plug (`POST /api/sbcs/<name>/ports`, the forms).
Power also checks boards sharing the board's plug outlet, SDWire, console
adapter or relay channel; assigning a port or plug checks the boards
already wired to that console or outlet. A claim counts as the caller's
when it belongs to the same user from any session (REST, the web UI, MCP
over HTTP), so an agent can claim over MCP and use its key on REST.

Operators may override: `"override_claim": true` in the JSON body (only
JSON `true` counts), or `?override_claim=1` in the query string (`1`,
`true`, `yes` or `on`; e.g. for `DELETE`), or the override button on the
web UI's refusal page. When the JSON body has an `override_claim` key, it
decides and the query string is ignored. The override is recorded in
the activity log as `claim_override`, as with the CLI's
`--override-claim`. Agents (`role: agent`) get `403 "not_operator"`, and
can't force-release a claim over REST or the web UI either. With
`claims.require_claim`, REST calls that change a board also need the
caller's claim (`409 "claim_required"`); the web UI doesn't (its users
are people, like the CLI's). Without auth every web/REST caller is the
same operator, so they share one claimant identity.

Deleting a board deletes the caller's own claim on it with it; a board
someone else has claimed is `409` (an operator's `override_claim` deletes
it anyway, even with `claims.enabled: false`, when, as with the CLI,
nothing is checked or logged as an override). Claiming over REST (`POST
/api/claims/<name>`) records the user as the claim's owner and refuses a
board whose shared hardware someone else holds, or one the caller already
holds (`409 "already_claimed"`: renew it instead). Durations (claim and
renew) must be whole minutes within the configured bounds.

Not checked: reading (every `GET`), the web serial console, health
checks, and the claims endpoints themselves.

**Power over REST:** `POST /api/sbcs/<name>/power` takes `{"action":
"on" | "off" | "cycle"}`, plus optional `"delay"` (seconds between off and
on for `cycle`: a number from 0 to 86400; the largest stored cycle delay
among the boards on the plug outlet, a board without one counting as 3.0,
is the default and the minimum, so a smaller value is raised to it;
`null` is refused) and
`"override_claim"`. In the order checked:

| Status | When |
|---|---|
| `404` | No such SBC |
| `400` | No power plug assigned, missing or bad `action`, or for `cycle` a bad `delay` or an out-of-range stored cycle delay (checked before claims) |
| `409` / `403` | Claim refusals as above (`sbc_claimed`, `claim_required`, `not_operator`) |
| `429` | A `cycle` while another REST or web-UI cycle of the same plug outlet (this board or one sharing it) is running in this server, or within 5 s of the outlet's last successful one |
| `409` | `{"error": ..., "switched": false}`: `on`/`off` while a REST or web-UI cycle of the board's outlet is running; or a pre_power relay (on this board or one sharing its outlet) couldn't be checked, or, for `on`, couldn't be set; power was not switched |
| `500` | The plug failed, or for `cycle` a strap couldn't be set after power-off (the board was left off) |
| `200` | `{"name", "action", "success": true, "state"}`, plus `"delay_seconds"` (the delay used) and `"delay_source"` (e.g. `"SBC minimum"`, `"minimum of 'b' (on the same outlet)"`, `"requested"`) for `cycle` |

`on` and `cycle` set the pre_power relays first, as the CLI and MCP do.
With several boards on one outlet, a `409` for `on` can follow relays that
were already set: only power is guaranteed unswitched. REST, the web UI's
power button, and MCP `power_cycle` follow the same delay rule as every
other power cycle: the board's stored cycle delay (`labctl edit
--cycle-delay`), or 3 s if none is set, is the default when no delay is
given and the minimum when one is (a smaller value is raised to it; a
negative one is an error). A cycle switches the whole plug outlet, so
when boards share it the largest of their minimums applies (a board
without a stored delay counts as 3 s, so another board's shorter stored
delay never takes it below 3 s), and an out-of-range one on any of them
refuses the cycle. The CLI's warning, MCP results and the REST
`delay_source` name the board the minimum came from. The same holds for CLI `power cycle`, the
reboot after `sdwire flash`/`update` (CLI and MCP), `boot-test` (CLI and
MCP) and the recovery commands. A stored delay outside 0-86400 s (stored
before `edit --cycle-delay` checked the range) is refused on every path
before anything is switched or written; clear it with `labctl edit <name>
--cycle-delay -1`. The web UI's power buttons follow the same
outlet rules as REST (a cycle in progress refuses another cycle and
on/off). That guard covers REST and the web UI of one server process
only: a power operation from the CLI, MCP, an `sdwire` reboot or
`boot-test` isn't seen by it, nor refused by it (a lab-wide outlet lock is
tracked in #38). A REST or web-UI cycle request returns only after the
whole off-wait-on sequence, so behind a reverse proxy set its read timeout
above the longest delay you store (plus a few seconds for the plug).
