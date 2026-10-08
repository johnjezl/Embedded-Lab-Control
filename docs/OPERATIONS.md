# Operation classification

Which labctl operations change what, so you know what is safe to run while
someone else is using the lab (issue #7, decision D012). There is no
enforced read-only tier yet: this is documentation. Access is governed by
`labctl` group membership, claims, and (for MCP) tool annotations plus the
two-step confirmation for destructive tools.

## Classes

| Class | Meaning |
|---|---|
| read | No writes at all (apart from a first-run DB schema setup) |
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
| `actuator_add` | db-write |  |  |
| `actuator_list` | read |  |  |
| `actuator_probe` | shared-resource |  | Briefly opens the relay's serial port; also records last_probe_* |
| `actuator_remove` | destructive | yes |  |
| `actuator_set` | destructive | yes |  |
| `add_sbc` | db-write |  |  |
| `add_serial_device` | db-write |  |  |
| `assign_power_plug` | db-write |  |  |
| `assign_serial_port` | db-write |  |  |
| `bind` | db-write |  |  |
| `bindings_list` | read |  |  |
| `boot_test` | destructive | yes |  |
| `claim_sbc` | db-write |  |  |
| `enter_recovery` | destructive | yes |  |
| `exit_recovery` | destructive | yes |  |
| `flash_image` | destructive | yes |  |
| `force_release_sbc` | destructive | yes |  |
| `get_claim` | read |  |  |
| `list_claims` | read |  |  |
| `power_cycle` | destructive | yes |  |
| `power_off` | destructive | yes |  |
| `power_on` | hardware |  |  |
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
| `run_health_check` | hardware |  | Pings / opens the ser2net port; writes status rows |
| `sdwire_add` | db-write |  |  |
| `sdwire_assign` | db-write |  |  |
| `sdwire_cat` | read |  | As sdwire_ls |
| `sdwire_discover` | read |  |  |
| `sdwire_info` | read |  | As sdwire_ls |
| `sdwire_ls` | read |  | Reads in place if the card is on the host; else switches only when the board is known OFF, and back |
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
scripted destructive commands. The exceptions are the older `remove`,
`sdwire remove`, `serial remove`, `actuator remove` and `power-all`, which
also ask (and abort without `--yes`) when stdin is not a terminal.
`power cycle`, `sdwire flash` and `sdwire update` also take `--dry-run`.

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
| `edit` | db-write |  |  |
| `enter-recovery` | destructive | yes | Power off, assert strap, power on |
| `exit-recovery` | destructive | yes | Power cycle with the strap released |
| `export` | read |  | Writes a file only with `-o` |
| `force-release` | destructive | yes | Overrides another user's claim |
| `health-check` | shared-resource |  | Opens each ser2net port briefly; writes status rows only with `-u` |
| `import` | db-write |  | Skips existing SBCs; `-u` overwrites their ports/addresses/plugs |
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
| `power cycle` | destructive | yes | `--dry-run` |
| `power off` | destructive | yes |  |
| `power on` | hardware |  |  |
| `power status` | read |  |  |
| `power-all` | destructive | yes | Every SBC (or a project) at once |
| `proxy list` | read |  |  |
| `proxy start` | shared-resource |  | Holds the console session; writes proxy state/logs; `--allow-write` lets clients type |
| `release` | db-write |  |  |
| `remove` | destructive | yes | Deletes the SBC and its data |
| `renew` | db-write |  |  |
| `request-release` | db-write |  |  |
| `sdwire add` | db-write |  |  |
| `sdwire assign` | db-write |  |  |
| `sdwire cat` | hardware |  | Switches the SD card to the host and back (see note below) |
| `sdwire discover` | read |  |  |
| `sdwire dut` | hardware |  |  |
| `sdwire flash` | destructive | yes | Overwrites the whole SD card; `--dry-run` |
| `sdwire host` | destructive | yes | Pulls the card from the board (`--force` skips the power check) |
| `sdwire info` | hardware |  | As `sdwire cat` |
| `sdwire list` | read |  |  |
| `sdwire ls` | hardware |  | As `sdwire cat` |
| `sdwire remove` | destructive | yes | Deletes a record |
| `sdwire unassign` | destructive | yes | Deletes an association |
| `sdwire update` | destructive | yes | Copies/renames/deletes files on the card; `--dry-run` |
| `ser2net generate` | system-write |  | Writes `/etc/ser2net.yaml` only with `--install` (or `-o` file) |
| `ser2net reload` | system-write |  | Restarts ser2net: drops every live console session |
| `serial add` | db-write |  |  |
| `serial capture` | shared-resource |  | Holds a ser2net session; sends nothing |
| `serial discover` | read |  | Prints only; registers nothing |
| `serial list` | read |  |  |
| `serial remove` | destructive | yes | Deletes a record |
| `serial rename` | db-write |  |  |
| `serial repair` | db-write |  | Dry run unless `--apply` |
| `serial send` | destructive | yes | Types into a running board's console (no proxy guard) |
| `serial udev` | system-write |  | Writes udev rules only with `--install`; reloads with `--reload` |
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

**CLI vs MCP:** the CLI `sdwire ls`/`cat`/`info` still switch the card to
the host and back without the MCP tools' strict power check (they proceed
when the board has no power plug). The MCP tools were hardened first because
agents may call them unprompted; aligning the CLI is tracked in the Phase 1
notes.
