# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed (P2 release prep)
- A command whose optional dependency is missing now says which extra to
  install, in one line, instead of a traceback or `pip install <package>`:
  `labctl web` (and REST) and `labctl user hash-password`/`add`/`verify`
  (werkzeug) → `[web]`, `labctl mcp` → `[mcp]`, Kasa plugs → `[kasa]`,
  SDWire → `[sdwire]`, USB relays → `[actuators]`. Each message gives
  `pip install "embedded-lab-control[<extra>]"` and the `pipx inject`
  equivalent with the extra's version floors for the running Python;
  the same text reaches the CLI, REST and MCP (`labctl/extras.py`).
  Below Python 3.12 the SDWire message says it needs 3.12+ (the extra
  installs nothing there) and gives no install command. Only the extra's
  own package being absent is reported this way: an installed but broken
  or incompatible one gets its own one-line error carrying the real
  ImportError and a reinstall hint (still an error line, not a
  traceback). A missing Kasa library
  no longer also logs a "Failed to load Kasa credentials" warning, and
  isn't remembered for the life of the process.
- README: install from PyPI with pipx or pip, a table of what each extra
  enables, and Python version notes; the source install and the
  systemd install from a git checkout (`scripts/install-services.sh`)
  are unchanged.
- `docs/HARDWARE_MAP.md` is now a generic guide to mapping USB serial
  adapters (USB paths, `labctl serial discover/add/udev`, assigning
  ports, troubleshooting) with placeholder paths and names.
- `scripts/install-udev.sh` requires the rules file as its argument (it
  defaulted to a repository file that no longer exists) and refuses a
  file with no `SYMLINK` assignment or with a non-comment line that
  isn't a udev rule, such as the message `labctl serial udev` prints
  when no adapters are registered. The README and `HARDWARE_MAP.md` note
  that installing a rules file by hand skips the claim check
  `labctl serial udev --install` makes. `labctl ports -v` with no ports points
  at `labctl serial discover` instead of `discover-usb-serial.sh`.
- The source distribution now includes the tests, `config/` (sample
  config, systemd units, udev mapping example), `docs/`, `scripts/`,
  `skills/`, `CHANGELOG.md` and `SECURITY.md` (`MANIFEST.in`); the wheel
  is unchanged. The test suite passes from an unpacked sdist.
- CI runs on pushes to `main`, on pull requests (so a PR branch is
  tested once, not twice) and by hand (`workflow_dispatch`).

### Removed (P2 release prep)
- This lab's `config/udev/99-lab-serial.rules`,
  `config/udev/port-mapping.yaml` and `config/ser2net/labctl-ser2net.yaml`.
  Rules are generated with `labctl serial udev` and the ser2net config
  with `labctl ser2net generate`; `config/udev/port-mapping.yaml.example`
  (now with placeholder paths and names) remains for the legacy
  `generate-udev-rules.py`.

### Added (#2: long calls keep the caller's claim alive)
- A claim's deadline moved only when a call started, so one long call
  (a 10-run `boot_test`, a long `serial_capture`, flashing a large
  image) could outlast it, and another agent could then claim the board
  and power-cycle it mid-run. MCP `boot_test`, `serial_capture`,
  `sdwire_update` and `flash_image`, and CLI `boot-test`, now heartbeat a
  claim the caller already holds on the board from a background thread
  while they run (every quarter of the claim's current duration, 5-60 s,
  so a renew to a shorter claim is followed). They never claim a board
  for the caller. Each heartbeat is one conditional UPDATE
  (`ResourceManager.heartbeat_held_claim`, which replaces the unused
  `heartbeat_claim`) that never revives a released, expired or
  taken-over claim; `touch_claim` now says whether a row changed. A
  database error is a missed heartbeat (the call goes on) until the
  claim's deadline passes without a successful one, or, for a caller
  holding no claim, until the checks have failed for 60 s in a row; if
  the claim can't be read when the call starts, it runs without
  keepalive (logged). The keepalive follows the claim the call started
  with (its heartbeat matches that claim's id): a claim released and
  taken again, even by the same user, counts as lost.
- Boot tests check again before each run's power cycle and stop once
  someone else claims the board or a board sharing its hardware, the
  caller's claim is lost (released, force-released or expired), or, over
  MCP with `claims.require_claim`, nobody holds it. The check runs
  before the heartbeat, so a refused check extends nothing. A stopped
  test says `STOPPED after N of M runs: <reason>` and reports
  `Result: INCOMPLETE, ...` (or `Result: STOPPED before the first run`,
  noting that the board is left off if the deploy powered it off)
  instead of a pass rate; MCP results start with `Stopped: <reason>` and
  the CLI exits 1. A CLI test started with `--override-claim` isn't
  stopped by the claims it overrode, but is by any claim taken after it
  started.
- `sdwire_update` and `flash_image` with `reboot=true` leave the board
  off, and say why, when the check fails after the card is written:
  `Not power cycled: <reason>` (`Not powered on:`) when the caller's own
  claim was lost, `<board> is left off (not power cycled) because
  '<board>' is now claimed by '<agent>'` when someone else claimed the
  board or a board sharing its hardware.
- `/deploy-and-test` skill: claim after a successful build, sizing the
  claim for the run (for `boot_test`, at least runs × (timeout + cycle
  delay + a few seconds) plus the deploy and a margin); renew it between
  long steps; release it on every path (deploy failure, boot timeout, a
  confirmation not given). Resolves the claims spec's open question on
  the skill.

### Changed (#41: options and config keys that did nothing)
- `serial.base_tcp_port` (and `LABCTL_BASE_TCP_PORT`) now sets where TCP
  port auto-assignment starts (`port assign` without `-t`, MCP, REST, web,
  `import`): one above the highest port in use, and at least the base. It
  must be 1-65535 (anything else keeps 4000, with a warning). The web
  settings page showed it blank (it read `ser2net.base_tcp_port`).
- `ser2net generate` no longer gives every port without a TCP port
  4000. With `--install` each such port is assigned one and saved, as
  `port assign` without `-t` would (one above the highest in use, at
  least `serial.base_tcp_port`; refused past 65535), and the command
  says which, with `port assign`'s claim checks (one shared helper). If
  any such port is on a board another user has claimed, or one sharing
  a claimed board's console device, `--install` refuses and saves and
  writes nothing (leaving the port out would change that board's
  console), listing them; `--override-claim` (new; recorded in the
  activity log) assigns them anyway. Claimed boards whose ports already
  have a TCP port are written as before. Nothing is saved unless the
  config file can be written (checked first), and each save repeats
  `port assign`'s check, so a claim taken meanwhile still refuses; any
  failure after a save lists the TCP ports already saved. A port whose
  save fails is left out and reported; the config is still written and
  the command exits 1.
  Without `--install` nothing is saved: those ports are left out of the
  output and the number each would get is shown (stderr).
- `ser2net generate --install` writes `ser2net.config_file` (default
  `/etc/ser2net.yaml`, unchanged); it always wrote `/etc/ser2net.yaml`.
  The write is atomic when labctl can create files in the directory (a
  temporary file there, given the old file's owner and mode, fsynced and
  renamed over it), and otherwise rewritten in place (e.g. a labctl group
  member, who can write `/etc/ser2net.yaml` but not `/etc`); a symlink on
  the path or a hard-linked file is refused, since it may run as root. The
  permission-denied message names the configured path; when the file is
  really absent from a directory labctl can reach, it says to create it
  with `sudo labctl ser2net generate --install` (and, for
  `/etc/ser2net.yaml`, then rerun `install-services.sh` or `update.sh`
  to make it group-writable); when the path can't be reached, it says so.
  `install-services.sh`, and now `update.sh` (for a file created after the
  install), make only `/etc/ser2net.yaml` group-writable (`root:labctl
  664`; a symlink there is left alone).
- `health.status_retention_days` now prunes status history: the monitor
  deletes older entries at startup and then hourly; `0` keeps all of it.
  Each board's newest entry per status, and the one in effect at the
  cutoff, are always kept, so a board stable for longer than the
  retention keeps its uptime. Schema v12 indexes `status_log(logged_at)`.
  An hourly run is cheap lookups (newest row, rows past the cutoff, all
  rows) and prunes only when one changed since the monitor's last prune.
  Pruning deletes in transactions of at most 5,000 rows, so the first
  prune of a long history doesn't hold the write lock for long.
- Uptime (`GET /api/sbcs/<name>/uptime`): the 24-hour figure starts from
  the status in effect 24 hours ago, so a board up the whole day shows
  100% (it showed 0% with no status change in the window), and times are
  compared in the database's clock (UTC), not local time.
- `claims.require_agent_name` is enforced: MCP `claim_sbc` and
  `POST /api/claims/<sbc>` refuse (`agent_name_required`) a claim that
  would be recorded under a placeholder name: no `agent_name` and no
  authenticated user (or, over MCP stdio, no name given earlier in the
  session). CLI claims are always named (`--name` or `$USER`). A REST
  `agent_name` that isn't a string is a 400.
- MCP over unauthenticated HTTP no longer remembers an `agent_name`
  across calls: the server can't tell its clients apart, so one client's
  name named every other client's claims and releases. It is used for
  the call that gives it; stdio and per-user (authenticated) names are
  still remembered.
- MCP `claim_sbc` defaults `duration_minutes` to
  `claims.default_duration_minutes`, like the CLI and REST (it was 30).
- `post-power` bindings can no longer be created: nothing acts on them
  yet. `bind --phase` no longer offers `post-power`, and labctl
  (`ResourceManager`) refuses `post_power`; MCP `bind` refuses it before
  its claim checks. Bindings
  stored with it before are kept and listed as inactive (`bindings list`
  marks them `*`, `bindings status` says so, MCP output carries
  `sample_phase_note`).
- `ports --all` lists the USB serial devices (`/dev/ttyUSB*`,
  `/dev/ttyACM*`) no `/dev/lab` symlink points at; the flag did nothing.
- `remove`, `sdwire remove`, `serial remove`, `actuator remove` and
  `power-all` ask only on a terminal. Without one they no longer read the
  answer from stdin (a piped `y` used to confirm) and exit 1 asking for
  `--yes`. Declining `actuator remove` now exits 1, like the others (it
  printed "Aborted." and exited 0).
- `install-services.sh` and `update.sh` make the services' database
  writable by the `labctl` group, so group members can write it from the
  CLI without `sudo -u labctl`: a new config puts it in
  `/var/lib/labctl/db` (setgid `2775`; the file, created empty if
  missing, and any `-wal`/`-shm` are `664`; SQLite gives new `-wal`/`-shm`
  files the database's mode). A database configured elsewhere is left
  alone, with a note. `database_path` is compared with the directory
  resolved (a symlinked `/var/lib/labctl` still matches; the database file
  itself is never followed). The labctl home gets group search (`g+x`,
  nothing else) when it belongs to the `labctl` group and lacks it; if
  group members still can't traverse it, the scripts warn. Both scripts
  run the same root-safe step,
  `python -m labctl.core.admin_files shared-db` (new; also new: `create`),
  and a problem there (a symlinked database file, a non-regular `-wal`, a
  config that won't parse) is a warning: the scripts carry on, and
  `update.sh` still restarts the services.
  **Upgrading:** an existing install keeps its database where it is
  (e.g. `/var/lib/labctl/labctl.db`). To share it with the group, as root
  throughout (your account may not see into `/var/lib/labctl`): stop the
  services, move `labctl.db` (and any `labctl.db-wal`/`-shm`) into
  `/var/lib/labctl/db/`, check it arrived (`sudo ls -l
  /var/lib/labctl/db/labctl.db`; if it isn't there, `update.sh` creates a
  new, empty database), set `database_path: /var/lib/labctl/db/labctl.db`
  in `/etc/labctl/config.yaml` and copy it to the service config, run
  `update.sh` (or `install-services.sh`), and start the services. README
  "Moving the database" has the commands.
- REST `POST /api/sbcs/<name>/power` returns 400, not 500, for a
  non-string `action` or a JSON body that isn't an object.

### Removed
- Config keys that were parsed but never acted on: `proxy.enabled`
  (and `LABCTL_PROXY_ENABLED`), `proxy.log_retention_days`,
  `proxy.idle_timeout` and `health.update_status_on_check` (use
  `monitor --no-update`). A config that still sets one loads, with a
  warning that it's ignored.

### Security
- MCP `sdwire_to_host` fails closed (#41): it switches the card only when
  the board's plug reports it off. It used to go ahead when the board had
  no plug or its state couldn't be read; it now refuses, like
  `sdwire_ls`/`cat`/`info`, unless `force=true`. CLI `labctl sdwire host`
  does the same: it refuses a board with no plug or an unreadable or
  unknown plug state unless `--force` (which, as before, skips the power
  check), and so do CLI `sdwire ls`/`cat`/`info` (they refused only a
  plug reporting ON). These now also read a card already on the host in
  place, as the MCP read tools do, and take `--force` to skip the check.
  One check (`labctl.sdwire.power_guard`) serves all of them. The MCP
  read tools' refusal points at `sdwire_to_host(force=true)` for a board
  you've made sure is off.
- A per-board cycle delay stored before `edit --cycle-delay` checked the
  range (e.g. `1e9`, NaN, negative) is refused on every path before
  anything is switched or written (#41): MCP/REST/web cycles, the reboot
  after `sdwire flash`/`update`, `boot-test` and the recovery commands, CLI
  and MCP. Only CLI `power cycle` checked it before; the others slept on
  it. Clear it with `labctl edit <name> --cycle-delay -1`.
- `labctl serial remove` and `labctl serial udev --install` check claims
  (#41). `serial remove` refuses an adapter that a board someone else has
  claimed has a port on, by any path that reaches it (including a port
  assigned by `/dev/ttyUSB0` behind the adapter's link, which isn't linked
  to the registration). `serial udev --install` refuses when the new rules
  would remove or re-point a `/dev/lab/<name>` link such a board uses
  (every link, if the installed rules file can't be read). Both take
  `--override-claim`. `serial remove` asks for confirmation only on a
  terminal; without one it refuses unless `--yes` (see the no-terminal
  entry above). MCP
  `remove_serial_device` refuses an adapter a board someone else has
  claimed has a port on, the same way.
- Writing a board's SD card fails closed when the board has no power plug
  (#41): `sdwire flash`/`update` (CLI) and MCP `sdwire_update`/`flash_image`
  used to switch the card silently; they now refuse unless `--no-plug-ok`
  (MCP `no_plug_ok=true`), which says you've made sure the board is off.
  The flag never skips the power-off of a board with a plug nor excuses a
  failed one. The MCP plans show `will_do.power_off`.
- Under `sudo`, the CLI acts for the user who ran sudo (`SUDO_USER`): its
  claim identity (`cli-<user>@<host>`), agent name and activity-log actor,
  so e.g. `sudo labctl serial udev --install` doesn't turn your own claim
  into someone else's.
- MCP confirmation tokens are bound to the plan they were issued with
  (`will_do`): if it differs at confirmation (e.g. the board's stored cycle
  delay changed), the call is refused and the caller asks for a new plan.
  A claimed board's power-cycling tools return the claim refusal, not a
  plan.
- REST and web-UI power cycles don't overlap: the guard and the 5 s rate
  limit are per plug outlet, so a cycle of an outlet (by any board on it)
  requested while another is running (it blocks for the whole delay) is
  refused with `429`, and `on`/`off` of that outlet meanwhile with `409`.
  The guard covers REST and the web UI in one server process only: power
  operations from the CLI, MCP, `sdwire` reboots or `boot-test` aren't
  seen by it (a lab-wide outlet lock is tracked in #38).
- `labctl edit --cycle-delay` (setting or clearing it) is claim-checked
  against the boards sharing the board's hardware as well, since a stored
  delay is the minimum of every board on the outlet. Other `edit` changes
  still check the board alone.

### Changed
- One cycle-delay rule on every surface (#41): the board's stored delay
  (`labctl edit --cycle-delay`), or 3 s if none is set, is the default
  and the minimum of every power cycle, not only CLI `power cycle` and
  recovery: MCP `power_cycle` (whose `delay` now defaults to it), REST
  `cycle` (likewise; the response adds `delay_seconds`), the web UI's
  power-cycle button, the reboot after `sdwire flash`/`update` and every
  `boot-test` run (CLI and MCP). A smaller requested delay is raised to
  it, so MCP and REST no longer allow less than 3 s on a board without a
  stored delay. Boards sharing a plug outlet are cycled together, so the
  largest of their minimums applies (each board's stored delay, or 3 s
  without one; an out-of-range one on any of them refuses), and the CLI
  warning, MCP results and REST `delay_source` name the board it came
  from. A negative requested delay is an error everywhere (CLI
  `power cycle --delay` used to raise it to the minimum). The `--dry-run` plans of
  `sdwire flash`/`update` and the MCP confirmations of `power_cycle`,
  `enter_/exit_recovery`, `boot_test` and `sdwire_update` (with
  `reboot=true`) show the delay and where it came from
  (`will_do.cycle_delay_seconds`, `cycle_delay_source`).
- Generated udev rules (`labctl serial udev`, and the legacy
  `scripts/generate-udev-rules.py`) make `/dev/lab/*` devices
  `root:dialout 0660`. They were world-writable (`0666`), so any local
  user could open a console or drive a relay. The labctl services are in
  `dialout` and ser2net runs as root; a user who drives relays from the
  CLI must now be in `dialout` (`labctl serial udev --install` says so
  when the user isn't). Installed rules change on the next
  `labctl serial udev --install --reload`.
- `install-services.sh` and `update.sh` make the service copy of the config
  (`/var/lib/labctl/.config/labctl/config.yaml`, which holds the same API
  keys and credentials as `/etc/labctl/config.yaml`) mode `640`; it was
  created world-readable (`644`), so any local user could read the keys
  wherever the labctl home is traversable by others (its mode comes from
  `useradd`'s `HOME_MODE`, `0750` on current Ubuntu/Debian, or from an
  admin: it is `2775` on the reference lab host).
- The install and update scripts (run as root) no longer follow symlinks
  or hard links planted under `/var/lib/labctl` (writable by the `labctl`
  service user) when they seed `/etc/labctl/config.yaml` from the service
  config, create the service config, change its owner and mode, or create
  and re-permission the `images`/`output` directories. These go through
  `labctl.core.admin_files`: no symlink on any path component below the
  (resolved) labctl home, single-link regular files only, descriptor-based
  ownership changes, `O_EXCL` creation, byte-for-byte copies. Previously a
  compromised service could have root hand it `/etc/labctl/config.yaml`,
  or copy an arbitrary root-only file into a config its group can read.
  Remaining by-path root operations there (`chown -R`, `mkdir -p`, the
  drift `cmp` and the allowlist config read) are tracked in #19.
- **MCP over HTTP now authenticates with the web users' API keys (breaking
  for HTTP clients when `auth.enabled` is true).** With auth enabled, every
  request to `labctl mcp --http` needs `Authorization: Bearer <api_key>`
  for a user in `auth.users` (401 otherwise); keys are re-read per request,
  so revoking one needs no restart. Sessions are bound to the user that
  opened them, destructive-tool confirm tokens to the user they were issued
  to, and each user is a separate claimant (one user's claim blocks
  another's mutating calls); claims and audit entries record the user. The
  server refuses to start without auth on a non-loopback `--host` or with
  non-loopback names in `mcp.allowed_hosts` / `allowed_origins` (a proxy
  that forwards `Host: 127.0.0.1` can't be detected: enable auth before
  adding one), with auth enabled but no API key configured, or when a
  config file it should read can't be loaded (a fallback to another file
  or to defaults could drop the authentication the intended config
  enables; this includes `/etc/labctl/config.yaml` being unreadable to a
  user outside the `labctl` group, who must then pass `-c`). A running
  MCP server (either transport) now keeps the configuration it started
  with until restarted; only API keys are re-read, from the same file, per
  request (a broken file refuses every key). Users
  without a string username can't authenticate by API key. New `mcp.allowed_hosts` / `mcp.allowed_origins` accept extra
  `Host` / `Origin` headers (e.g. a reverse proxy's public name) on top of
  the loopback names. See `docs/MCP_SERVER.md` (D012).
- API key lookups (web and MCP) no longer raise on a non-ASCII key in a
  request header, and a non-string `api_key` in the config never matches.
- **Destructive MCP tools now require a two-step confirmation (breaking for
  MCP clients).** The 23 tools annotated destructive (power off/cycle,
  flashing, SD updates, `serial_send`, deletions, actuator/binding verbs,
  recovery, force release) return a plan and a single-use `confirm_token`
  on the first call and act only when called again with the same arguments
  plus that token (120 s expiry, bound to tool and arguments). Configure
  with `mcp.confirm_destructive` (default `true`) and `mcp.confirm_exempt`.
  See `docs/MCP_SERVER.md` (D012).
- **MCP host file access is now allowlisted (breaking).** `flash_image`
  (`image_path`, `post_flash_copies` sources), `sdwire_update` (`copies`
  sources) and `boot_test` (`image`, `output_dir`) only accept absolute host
  paths inside the new `mcp.allowed_read_paths` / `mcp.allowed_write_paths`
  settings. Both default to empty, which denies all host file access over
  MCP. Paths are resolved (symlinks, `..`) before the check and are refused
  before any power or SD-mux change. Checked files and directories are
  opened at check time without following symlinks (`labctl.core.safe_open`)
  and all later I/O uses those handles, so a symlink swapped in after the
  check can't redirect it; raw images are streamed into `sudo dd` instead
  of being opened by root. Image format and copied file names follow the
  path given. The CLI is unaffected. Fresh installs
  get `/var/lib/labctl/images` (read) and `/var/lib/labctl/output` (write);
  `scripts/update.sh` creates those directories on existing hosts and prints
  the config to add (D012).
- Web UI: without a configured `auth.secret_key`, the session-signing key is
  now random per process. It used to fall back to the constant
  `labctl-dev-key` when auth was disabled; because the signed session
  carries the CSRF token, anyone could forge it. Browser sessions now reset
  when the web service restarts unless a key is configured.
- MCP `sdwire_ls` / `sdwire_cat` / `sdwire_info` no longer have side effects.
  A card already switched to the host is read in place and left there
  (host mode is confirmed with a one-sector `sudo dd` read, because the
  reader's reported size lags a switch in either direction); a
  card on the DUT side is switched to the host and back only when the
  board's power plug reports OFF. With no power plug, an unreadable plug, or
  an unknown state they now refuse (`power_unknown`) instead of switching
  the card away from a possibly running board. They are annotated read-only.
  Callers that relied on reading a plug-less SBC's card must switch it
  explicitly with `sdwire_to_host` first.
- Read-only SD card mounts (`sdwire_ls`/`sdwire_cat` and the CLI
  equivalents) no longer replay a dirty filesystem journal, which wrote to
  the card despite `-o ro`: ext2/3/4 mount with `noload`, XFS/F2FS with
  `norecovery`, btrfs with `rescue=nologreplay`.
- MCP `sdwire_to_host` now waits (up to 10 s) until the card is readable on
  the host before returning, and warns if it isn't, so an immediate
  follow-up read doesn't misjudge the mux position.

### Changed (documentation audit)
- README, CLI help, MCP tool descriptions and docs were audited against
  the code and corrected. Users will notice:
  - README examples that failed as written: `labctl power on <sbc>` (not
    `power <sbc> on`), `port assign` with its port type, `monitor` (no
    `--foreground`), `-r` on `claim`/`force-release`/`request-release`.
    The REST table lists the real routes, with the power endpoint's
    `delay`, `override_claim` and status codes (400/409 `switched:
    false`/429/500).
  - Install instructions use the extras from a checkout (`pip install -e
    ".[web,mcp,kasa,sdwire,actuators]"`): the package isn't on PyPI yet.
    The README gives the Python requirements, the config search order
    (a personal `~/.config/labctl/config.yaml` wins over `/etc`), and the
    `labctl serial udev` flow in place of `install-udev.sh`. The SDWire
    sudoers rule adds `parted` and `blkid` and gets its own file.
  - New README sections on relays, bindings and recovery (`pre_power`)
    and on the common flags (`--yes`, `--override-claim`, `--dry-run`).
  - `docs/MCP_SERVER.md` documents the actuator, binding and recovery
    tools; the prompt names are `debug_sbc` and `lab_report`; 24 tools
    need confirmation; every image-upload error is listed.
  - CLI help and MCP tool descriptions say what commands actually do,
    e.g. `sdwire flash`/`update` power the board off and leave it off
    without a reboot; `power on`/`cycle` set the `pre_power` relays of
    boards sharing the outlet too; MCP/REST cycles don't use the board's
    stored cycle delay; `actuator set` points at `labctl bindings
    actuate`; `bind --phase post-power` is stored but not acted on yet.
  - `SECURITY.md` lists the controls and states the web UI's default
    exposure: the shipped unit listens on `0.0.0.0` over HTTP, auth is
    off by default, and CSRF applies only with auth.
  - `config/labctl.yaml.example` lists the environment variables that
    actually override settings, and the `claims`, `proxy`, `health` and
    `database` keys and `mcp.allow_admin_actuator_ops` with their
    defaults.
  - `docs/OPERATIONS.md`: `run_health_check` is `shared-resource` (it
    writes nothing); claim-unchecked commands and aliases are complete.
  - `docs/HARDWARE_MAP.md` is a generic example; `SPEC_actuators.md`,
    `STATUS.md`, `CLAUDE.md` (schema v11) and `IMPLEMENTATION.md` match
    what was built; the `deploy-and-test` skill covers confirmation,
    claims and image upload.

### Fixed
- The SD card is no longer switched to the host while the board may be
  running (bus contention can corrupt the card). `boot-test` with an image
  (CLI and MCP) didn't power the board off at all; `sdwire flash` and
  `sdwire update` (CLI and MCP) powered it off on a best-effort basis and
  carried on when that failed. All of them now refuse, writing nothing,
  when the board can't be switched off. A `boot-test` deploy that fails
  once the board is off hands the card back to the board and says the
  board was left off (it used to be a traceback for errors other than
  RuntimeError).
- `mcp.allow_admin_actuator_ops` turns on only for a YAML boolean `true`.
  It was read with `bool()`, so a quoted `"false"` (or any other string)
  enabled `actuator_add`/`actuator_remove`/`actuator_set` over MCP; any
  non-boolean value now leaves them off, with a warning.
- `labctl edit --cycle-delay` refuses a delay over 86400 seconds, NaN or
  infinity (nothing is written; `-inf` no longer counts as "clear"). The
  stored delay is the default and the floor of `power cycle`, so such a
  value made every later cycle of the board fail, with no `--delay` able
  to get round it; `power cycle` now says how to clear one stored
  earlier.

- `pre_power` relay bindings (e.g. a `recovery_mode` strap) are now set
  before power-on on every path, not just CLI `power on`/`power cycle` and
  the recovery composites (#36). The MCP `power_on`/`power_cycle`/
  `boot_test` tools and the reboot after `sdwire_update`/`flash_image`,
  the web UI and REST power actions, and CLI `power-all on`, `boot-test`
  and the reboot after `sdwire flash`/`update` used to switch power and
  leave the strap however it was last set, so a board could boot in or out
  of recovery depending on which interface turned it on. Boards that share
  the plug outlet have their straps set too, since the outlet powers them
  all (this also applies to `enter-recovery`/`exit-recovery`). A relay
  that can't be opened, or whose channel is busy with another operation,
  now stops these before power is switched (cycles check before powering
  off; a flash or file update with a reboot checks before it powers the
  board off and writes the card): MCP and the CLI say so in one line, the
  REST API answers 409 with `"switched": false` when nothing was switched
  (a failure after power-off stays a 500), the web UI flashes it, after a
  flash or file update the tools report that the power cycle failed and
  why, and `boot-test` records the run as failed. A strap that fails after
  power-off leaves the board off, and the error says so. A power-cycle
  delay over 86400 seconds, or not a number, is now refused before
  power-off: by CLI `power cycle` (also with `--dry-run`, and when the
  board's stored delay is out of range; a delay below the board's minimum
  is still raised to it), and, with negative delays too, by MCP
  `power_cycle` and by the REST API with a 400. It used to switch the
  board off, then fail.
- A relay that labctl can't open no longer crashes `power on`/`power
  cycle` (and the binding verbs and recovery composites) with a
  traceback; nor does an actuator whose driver isn't implemented. The
  error is one `Error:` line, and the board is never powered on without
  its strap. `power cycle`, `enter-recovery` and `exit-recovery` now
  check every relay their pre-power bindings need before powering off,
  so a relay they can't open no longer leaves the board off. A
  permission error says what to do: join the group that owns the device
  (only when that group grants read/write and isn't root's), then start
  a new login and restart any service or MCP client running labctl. `labctl actuator set` and the MCP
  `actuator_set` tool report the same error instead of raising.
- Over stdio, the MCP `list_images` tool said only that uploads need the
  HTTP transport, which read as a dead end. It now lists what a stdio
  session (which runs on the lab host) can do instead, with this server's
  paths: pass an absolute path inside `mcp.allowed_read_paths`, copy the
  image into `mcp.upload_dir` (under a unique temp name, then renamed into place;
  it says whether the session can write there) and use its bare name, or
  upload to the host's HTTP service. The last two are offered only when
  bare names resolve (`upload_dir` inside `allowed_read_paths`). With
  uploads off or `upload_dir` unreadable, a stdio session is still pointed
  at host paths, and the server instructions no longer say files must be
  uploaded over HTTP.
- Kasa strips that only answer KLAP discovery (e.g. HS300 firmware 1.1.2,
  which closed the legacy port 9999) are controlled per outlet again.
  python-kasa types such a device by its family (`IOT.SMARTPLUGSWITCH`)
  as a single plug with no outlets: state reads failed with
  `KeyError: 'relay_state'`, and an on/off for any outlet was sent to the
  strip as a whole, not to that outlet. labctl now reads the sysinfo and
  lets python-kasa pick the class from it, as python-kasa's own `connect`
  does, rebuilding the device on the same connection when discovery
  picked another. A Kasa device that reports no outlets now refuses an
  outlet index above 1 instead of switching the whole device.
- Kasa power control works again with IOT devices whose firmware moved to
  KLAP login version 2 (e.g. HS300 hardware 2.0, firmware 1.1.2): every
  command failed with `AuthenticationError: Device response did not match
  our challenge` even with correct credentials, because python-kasa before
  0.12 always used KLAP v1 for IOT devices. The `kasa` extra now requires
  `python-kasa>=0.12` on Python 3.11+. python-kasa 0.8 and later need
  Python 3.11, so on 3.10 it pins 0.7.7 (it allowed 0.5, which lacks
  APIs labctl uses) and those devices still can't be controlled there.
  When an authentication error persists with credentials configured and
  python-kasa < 0.12, the error message now says so and names the upgrade.
- `labctl mcp --http` waits up to 15 s for a port that is in use before
  starting (seen once on a deploy: the port was still in use ~1 s after
  the old server had exited, cause unknown; the new server's start failed
  and systemd restarted it 10 s later). It probes the addresses uvicorn
  will bind, then lets uvicorn bind as before. `update.sh` also waits,
  for every service, while a unit is `deactivating`, `activating` (as
  while systemd restarts a failed start) or `reloading`, within
  `MCP_START_TIMEOUT` (now measured in seconds), and says when systemd
  had to restart one; it had reported `labctl-mcp` as failed.
- The test suite no longer loads the machine's real config. About 115
  tests loaded config without naming a file, so on a lab host whose user
  can read `/etc/labctl` they used the production config and database; a
  run during the image-upload work migrated a production database to a
  pre-release schema v10 (an `images` table without `file_id`, so uploads
  failed with 500). Schema v11 adds the missing column to such a
  database, so a backup taken in between can be restored safely. Every test, and every process it starts, now gets its own home
  and no system config. New `LABCTL_SYSTEM_CONFIG` environment variable:
  replaces `/etc/labctl/config.yaml` in the config search order.
- REST `DELETE /api/sbcs/<name>` on a claimed board answered `500`; it is
  now `409 "sbc_claimed"` (#21). A non-numeric `duration_minutes` on
  claim or renew answered `500`, now `400`; renew durations are now
  bounded like claims. The web plug form refuses an outlet index below 1
  (index 0 switches every outlet on a Tasmota device).
- REST and web claims used a session ID the next request often didn't
  share (a per-request ID without auth, an API-key prefix with it), so a
  claim made over REST could not always be renewed or released; they now
  use `web:<user>` (`web:anonymous` without auth) and record the user as
  owner. Claims made before the upgrade keep their old ID: release them
  with force-release if needed.
- A command given a config file by name (`-c FILE` or `LABCTL_CONFIG`)
  fails if it doesn't load (YAML error or invalid value) instead of
  running on built-in defaults (commands that don't read the config,
  e.g. `user generate-key` / `hash-password` / `add`, `completion` and
  `--help`, still run). `labctl mcp` handles this itself: over
  HTTP it refuses to start, over stdio it starts with a warning.
  `labctl web` refuses to start if any config file it reads doesn't load
  (e.g. `/etc/labctl/config.yaml` unreadable to a user outside the
  `labctl` group: pass `-c`), since it would otherwise serve without the
  login the intended config enables.
- A config value that is valid YAML but not a valid setting (e.g.
  `timeout_seconds: 10s`, or a list at the top level) is a load error
  (warning; the next file in the search order, or built-in defaults)
  instead of a crash.
- An MCP `boot_test` `output_dir` that is an existing file is reported as
  "not a directory" instead of a possible race.
- A config section header with nothing under it (e.g. `mcp:` followed only
  by commented-out entries) no longer crashes every labctl command; it
  counts as an empty section. A section that isn't a mapping is ignored
  with a warning.
- Boot-test per-run output files (`run_NN.txt`) are written with
  `O_NOFOLLOW` and refused if hard-linked, so a link planted in a shared
  output directory can't redirect the (DUT-controlled) output into another
  file. The default `/var/lib/labctl/output` is now also sticky (`3775`).
  A run file that can't be written no longer fails the whole boot test;
  the summary (also the MCP result) lists every file that wasn't saved.
- `labctl -c FILE mcp` now makes the MCP server use FILE. The server loaded
  its config itself and ignored `-c`, so it read
  `~/.config/labctl/config.yaml` first (for the systemd unit: the service
  user's config, not the `/etc/labctl/config.yaml` the unit names). This
  matters now that the MCP host-file allowlist lives in config. FILE is
  exclusive, as with any explicit path: never another config. The same
  exclusive mode is available as `LABCTL_CONFIG_EXCLUSIVE=1` alongside
  `LABCTL_CONFIG`. (How a running server treats later changes to FILE:
  see Security, MCP over HTTP.)
- MCP claim sweeper (issue #3): a crashed `mcp-stdio` session whose PID was
  reused by another process is now recognised as gone (the process start
  time is compared with the session's epoch), so its claim is released as
  `session-lost` rather than later as `expired`.

### Changed
- Kasa: driver errors (bad outlet, unreachable plug) inside the MCP server
  are reported as such, not as an unrelated asyncio error.
- Power plugs: `labctl plug assign --index` and MCP `assign_power_plug`
  refuse an outlet index below 1 (Tasmota's `Power0` switches every
  relay). Rows saved with such an index (earlier, or by `labctl import`)
  count as sharing every outlet on that address for claim checks.
- MCP `serial_capture` is claim-checked like `serial_send`: it holds the
  board's ser2net session.
- HTTP MCP sessions are labelled `mcp-http` (claims' `session_kind` and
  activity-log actors), not `mcp-stdio`; the dead-session sweep covers both.
- **Package renamed for PyPI**: the distribution is now
  `embedded-lab-control` (`pip install embedded-lab-control`); the import
  package and CLI remain `labctl`. Version `0.2.0.dev0`, single-sourced from
  `labctl.__version__` (also used by `/api/health` and the web footer, which
  previously hard-coded `0.1.0`). License metadata is now the SPDX
  expression `MIT`, and the LICENSE file ships in the wheel and sdist.
- **MCP server migrated to the mcp 2.x SDK** (`mcp>=2.2,<3`, `MCPServer`
  replaces `FastMCP`). `mcp>=1.0` had started resolving to 2.2, where
  `mcp.server.fastmcp` no longer exists and `labctl mcp` crashed on import.
  Tools, resources and prompts are unchanged. mcp 2.x runs sync tools and
  resources on worker threads; labctl keeps 1.x semantics by serializing tool
  calls and the hardware-touching resources (`lab://power/{sbc}`,
  `lab://health/{sbc}`, `lab://status`) under one lock (D011). The server
  now reports labctl's version in `serverInfo`.
- `scripts/update.sh` and `scripts/install-services.sh` uninstall the old
  `labctl` distribution before installing `embedded-lab-control`, so the
  first upgrade doesn't leave two distributions owning `labctl/` (a later
  `pip uninstall labctl` would otherwise delete the new install).
- The `sdwire` extra only installs on Python ≥ 3.12 (PyPI `sdwire` requires
  it). On 3.10/3.11 SDWire commands report that the package is missing.
- Development: `src/` and `tests/` are now clean under `black`, `isort` and
  `flake8`, which are pinned exactly in the `dev` extra
  (`black==26.5.1`, `isort==9.0.1`, `flake8==7.4.1`). Formatting-only apart
  from removing dead code flagged by flake8 (unused imports/variables).

### Added
- MCP `serial_send` takes `keys` (named keys: ESC, Enter, Tab, Space,
  Backspace (0x08), arrows, Home/End/Insert/Delete/PgUp/PgDn, F1-F12,
  Ctrl-A..Ctrl-Z, in xterm encodings; each key is written 50 ms after
  the previous one, for consoles that time out a lone ESC quickly) and
  `data_hex` (raw bytes, e.g. for firmware whose terminal type wants
  other function-key codes, or 0x7F to erase at a Linux prompt), so
  agents can drive UEFI/U-Boot menus: a raw ESC in `data` is dropped
  before it reaches the server (#33). `newline` defaults to off when
  either is given. Its confirmation now shows the exact bytes it will
  send (`will_do`), and an unknown key or bad hex is refused before a
  token is issued. F11 has been checked on a Jetson's UEFI; the other
  encodings have not been checked on real firmware yet.
- **Image uploads over MCP HTTP** (D014): `PUT /images/<name>` on the MCP
  HTTP port, with the same API key, stores a file in the new
  `mcp.upload_dir` (default empty: off); `flash_image`, `sdwire_update`
  copies and `boot_test` take an uploaded image's bare name. New tools
  `list_images` (with upload instructions) and `delete_image`. Limits:
  bare names, `mcp.max_upload_mb` (8192), 1 GiB kept free, optional
  `?sha256=` check, atomic replace with `?overwrite=1`. Only the uploader
  or an operator may replace or delete an image; uploads and deletions are
  in the activity log. Schema v10 (`images` table).
- **TLS for the MCP HTTP server**: `labctl mcp --http PORT --cert FILE
  --key FILE`, or `mcp.cert_file`/`mcp.key_file`. A non-loopback bind
  without TLS logs a warning. `labctl mcp --check` prints the `tls:` and
  `uploads:` settings.
- Claim enforcement, part 3 (D013): the web UI and REST API refuse to
  operate or reconfigure a board another user has claimed (REST: `409`
  `"sbc_claimed"`; web UI: a page naming the holder), including boards
  sharing its hardware and boards already wired to a port or plug being
  assigned. Operators may override (`"override_claim": true`, or the
  button on the refusal page), recorded as `claim_override`; agents
  (`role: agent`) get `403 "not_operator"`, also for force-release (which
  any REST or web user could do before). `claims.require_claim` applies
  to the REST API. REST claims record the user as owner, so one user is
  one claimant across REST, the web UI and MCP over HTTP. See
  docs/OPERATIONS.md.
- Claim enforcement, part 2 (D013): CLI commands that operate or
  reconfigure a board refuse when another user has claimed it (or a board
  sharing its plug outlet, SDWire, console adapter or relay channel), naming
  the holder, before any confirmation question. `--override-claim` acts
  anyway and is recorded in the activity log as `claim_override`;
  `power-all` skips claimed boards; `import` checks every board before
  changing any; `claim` refuses a board whose shared hardware someone else
  holds. `remove` releases the user's own CLI claim before deleting, and
  `remove --force` is now an alias for `--override-claim` (also logged).
  See docs/OPERATIONS.md for the list. Assigning a serial port a TCP port
  another board's port already uses is now refused (CLI, MCP, web): the
  two would share one ser2net console, reaching a claimed board's console
  through another board.
- Claim enforcement, part 1 (D013): MCP tools that change a board's
  configuration (`update_sbc`, `assign_*`/`remove_*` for ports, plugs and
  addresses, `sdwire_assign`/`unassign`, `bind`/`unbind`) now refuse under
  another user's claim, as do `actuator_set`/`actuator_remove` on channels
  bound to a claimed board. A claim also covers the board's hardware:
  operating a board that shares a plug outlet, SDWire, serial device or
  relay channel with a board someone else claimed is refused, as are
  claiming it and assigning such hardware to another board.
- `auth.users[].role`: `operator` (default) or `agent`. Only operators may
  `force_release_sbc`; agents get `not_operator`.
- `claims.require_claim` (default `false`): MCP calls that change a board
  refuse with `claim_required` unless the caller has claimed it.
- Claims record their authenticated owner (schema v9): the same user's
  other MCP sessions, and the same user after a server restart, count as
  the claimant (use, renew, release). Claiming a board you already hold
  returns `already_claimed` and renews it for the requested duration
  (boards sharing its hardware aren't re-checked).
- `docs/CLAUDE_CODE_AGENTS.md`: connecting Claude Code agents to the HTTP
  MCP server with one labctl user and API key each, so claims keep them
  apart. Covers `claude mcp add` (manual) and a shared `--mcp-config` file
  with the key taken from `LABCTL_API_KEY` at launch (streamlined), agent
  instructions, identity checks and key revocation.
- `SECURITY.md`: how to report vulnerabilities privately, and what's in
  scope. `docs/MCP_SERVER.md` opens its security section with an overview
  of every control (defaults, settings), known limits and a deployment
  checklist; the README links them.
- `labctl mcp --http PORT --check` runs the HTTP startup checks (the same
  code the server runs) without serving: whether clients need API keys
  (`auth: required` / `none`), the config file loaded, and startup
  warnings; exit 1 if the server would refuse to start or a config file it
  would read is unparseable, unreadable, or (when named by
  `LABCTL_CONFIG`) missing. A `-c FILE` that is missing or unreadable is
  rejected by the CLI itself (exit status 2).
- `scripts/update.sh`, after restarting, shows `labctl-mcp`'s journal from
  this restart (auth mode, startup warnings) and, whenever auth is
  enabled, reminds that HTTP clients must send their API key; for any
  service that failed to start it prints that service's log since the
  restart. Before installing anything it refuses a config that doesn't
  load (YAML error or an invalid value). The MCP server prints its auth
  mode on every HTTP start and
  logs startup warnings (short keys, unknown confirm exemptions, unchecked
  Host headers; over stdio also config files it couldn't load) whatever
  `log_level` says; over HTTP an unloadable config makes it refuse to
  start instead (see Security).
- Destructive CLI commands (19: `power off`/`cycle`, `sdwire flash`/`update`/
  `host`/`unassign`, `serial send`, `actuator set`, binding verbs, recovery,
  `force-release`, `boot-test`, record removals) take `--yes/-y` and ask for
  confirmation, but only when stdin is a terminal: cron, CI, systemd, pipes
  and agents' tool calls are not asked. Scripts run from an interactive
  shell inherit the terminal and will be asked: add `--yes` to them. `power cycle`, `sdwire flash` and `sdwire update` take
  `--dry-run` (validate and print the plan; change nothing).
- `docs/OPERATIONS.md`: every CLI command, MCP tool and MCP resource
  classified as read / db-write / shared-resource / system-write /
  hardware / destructive (issue #7). A test keeps it in sync with the code.
- MCP tool annotations (`title`, `readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) on all 52 tools, defined in one table in
  `mcp_server.py`. A unit test fails if a tool is added without an entry.
- `labctl mcp --host ADDR` (default `127.0.0.1`) for the HTTP transport.
- `labctl mcp` without the `mcp` extra now prints an install hint instead of
  a traceback.
- GitHub Actions CI (`.github/workflows/ci.yml`) on push and pull request:
  Python 3.10–3.13 matrix running flake8, black, isort and pytest with
  coverage (XML uploaded as an artifact), plus a gitleaks scan of the full
  git history. Reviewed gitleaks false positives are listed in
  `.gitleaksignore`.
- `labctl serial send` send pacing to avoid receiver UART RX-FIFO overrun
  on sustained sends (issue #8). A gapless burst can leave a receiver no
  window to drain its RX FIFO, dropping a contiguous mid-payload byte run.
  Two new mutually-exclusive knobs feed the payload to ser2net in delayed
  slices within a single TCP connection:
  - `--interbyte-delay-ms N` — one byte at a time, N ms between bytes.
  - `--chunk-size B --chunk-delay-ms N` — B bytes per slice, N ms between.
  Pacing applies to both send-only and `--capture` paths. Hardware/software
  flow control (RTS/CTS, XON/XOFF) is tracked separately in issue #9.

### Fixed
- `labctl mcp --http PORT` ignored PORT and always bound `127.0.0.1:8000`.
  It now binds `127.0.0.1:PORT` (or `--host`). **Deployment note:** the
  shipped systemd unit uses `--http 8080`, so a deployed server moves from
  `:8000` to `:8080`. With a loopback bind, the SDK's DNS-rebinding guard
  rejects non-localhost `Host` headers (HTTP 421).
- `labctl.sdwire` failed to import on Python 3.10 (`datetime.UTC` is
  3.11+), despite `requires-python >= 3.10`.
- Hotfix: per-connection SQLite pragmas (`busy_timeout`, `synchronous=NORMAL`,
  `foreign_keys=ON`) are now best-effort. After the WAL switch, callers
  without OS-level write permission on the DB file (e.g. users outside
  the `labctl` group running `labctl serial send`) hit
  `sqlite3.OperationalError: attempt to write a readonly database`
  at connection open, because SQLite auto-downgrades the connection to
  read-only and then rejects write-side pragmas. We now wrap each
  pragma in `try/except sqlite3.OperationalError` and log at debug.
  Writable connections still get full tuning; read-only callers keep
  working. Regression test added in `tests/unit/test_database.py`.
- Transient `sqlite3.OperationalError: database is locked` errors under
  multi-process load (monitor daemon + CLI + MCP + batch jobs hitting
  the same SQLite file). Root cause was the combination of default
  rollback-journal mode (readers and writers exclude each other across
  the whole DB), the default 5s `sqlite3.connect()` busy timeout, and
  `Database.initialize()` re-running its `sqlite_master` probe on every
  `get_database()` call. Fix:
  - `PRAGMA journal_mode=WAL` applied on first `initialize()` (persists
    in the DB file header — concurrent processes inherit it). Readers
    no longer block writers and vice versa; only writer-vs-writer is
    serialized, and labctl writes commit sub-millisecond.
  - `PRAGMA synchronous=NORMAL` (WAL-safe pairing, faster commits, a
    few-second worst-case write loss on power failure — fine for a lab
    DB).
  - Per-connection `busy_timeout` mirrors `Config.database.timeout_seconds`
    (new; default **10s**), and the same value is passed as
    `sqlite3.connect(timeout=…)`. Backstops the narrow windows WAL
    still serializes (schema migrations, WAL checkpoints).
  - `initialize()` is now cached per-process: subsequent calls on the
    same `Database` instance return immediately without re-opening a
    connection. Long-lived processes (daemon, MCP, web) pay the
    sqlite_master probe exactly once; short-lived CLI invocations still
    pay one initialize() per run.
  - New `database` config section (`database.timeout_seconds`, default
    10.0) exposed via `Config.database`. Wired through `get_database` /
    `get_manager` and the four call sites (CLI, web app, MCP server).
  - Note: WAL mode creates `-wal` and `-shm` sidecar files next to the
    DB file. Backup scripts that previously copied just `labctl.db` now
    need to include those sidecars (or quiesce the daemon and run
    `PRAGMA wal_checkpoint(TRUNCATE)` before the backup).
  - `LABCTL_DATABASE_TIMEOUT` env var overrides
    `database.timeout_seconds` at load time, parallel to the other
    `LABCTL_*` overrides. Useful for ad-hoc tuning under contention
    without editing config.yaml.
  - `INSERT OR IGNORE` on the `schema_version` bootstrap inserts
    eliminates a latent cross-process race where two fresh-DB
    initializers could both pass the `sqlite_master` probe and the
    losing one would fail with a PRIMARY KEY conflict. Independent
    of the lock fix but the WAL change made it slightly more likely
    by shrinking the serialization window.
  - `Database._init_lock` is a `threading.RLock` (not `Lock`) so a
    callback into `initialize()` while the lock is held does not
    self-deadlock. Defensive — nothing in the current codebase
    triggers re-entry.

### Added (actuators feature, P1–P7, PR #4)

First-class support for USB-controlled relays as provisioned lab
resources, with per-target purpose **bindings** (recovery_mode,
boot_select, power_button, …) so verbs are semantic
(`labctl enter-recovery jetson-nano-2`) rather than device-specific.
Built per `docs/SPEC_actuators.md`. Since #37, `pre_power` straps are
set on every power-on path (see Fixed above).

- **Schema v8** — `actuators`, `actuator_channels`, `bindings` tables
  with CHECK constraints on enum columns, UNIQUE on
  `actuator_channel_id` (one binding per channel), UNIQUE on
  `(sbc_id, purpose)` (one purpose per SBC). Migration v7→v8.
- **Driver layer** (`labctl.actuators`):
  - `RelayDriver` ABC with structured `WriteOutcome` /
    `ProbeResult` so composites can abort safely on USB drop mid-sequence.
  - `lcus1_serial` — CH340-based 4-byte ASCII protocol at 9600/8N1,
    write-only, software-tracked state, side-effect-free probe.
  - `MockRelayDriver` for tests with programmable failure modes.
  - `get_driver()` registry; future drivers raise NotImplementedError.
- **Runtime layer** (`labctl.actuators.runtime`):
  - Per-channel non-blocking locks ⇒ ChannelBusyError for concurrent
    verbs; verb/shape congruence enforced (BindingShapeError).
  - `actuate_binding` / `release_binding` / `press_binding` —
    desired_state persisted only on WriteOutcome.OK.
  - `apply_pre_power_bindings` — re-applies pre_power straps before
    `power_on` / `power_cycle` so recovery_mode survives power cycles.
  - `enter_recovery` / `exit_recovery` — pre-flight probe + power_off
    + sleep + (de)assert + power_on, with explicit abort if the
    actuator is unreachable BEFORE any power transition.
  - `apply_safe_drive_on_startup` — daemon-start reconciliation that
    leaves `desired_state=asserted` channels alone (recovery
    mid-flash survives a daemon restart) and drives everything else
    to `default_state`.
- **CLI**:
  - `labctl actuator add/list/probe/remove/set`
  - `labctl bind`, `labctl unbind`, `labctl bindings list`
  - `labctl bindings actuate / release / press / status`
  - `labctl enter-recovery / exit-recovery`
  - `power on` / `power cycle` now consult pre_power bindings.
- **MCP**: complete tool set — provisioning, binding management,
  operation verbs (claim-gated), and recovery composites.
- **SBC claim composition**: `claim_sbc` snapshot-checks per-channel
  locks; refuses with ChannelBusyError if a verb is mid-flight.
- **Wiring-polarity validation** at bind time: refuses
  `default_state == active`.
- **`pyproject.toml`**: new optional `actuators` extra pulling
  `pyserial>=3.5`.

Tests: **897/897 pass.** ~140 new tests across schema migration,
manager CRUD, driver ABC contract, LCUS-1 frame builder + I/O,
runtime verbs, composite power integration, daemon-start safe-drive,
claim composition, CLI surface, and MCP tools.

#### Post-review hardening (PR #4)

- Hardware writes from automatic power-flow paths now leave audit
  trails alongside operator-initiated verbs: `apply_pre_power_bindings`
  emits `pre_power_apply` events (one per strap driven); daemon-start
  `apply_safe_drive_on_startup` emits one synthetic `safe_drive`
  summary event with held/drove/failed counts and details.
- `_validate_actuator_device_path` now rejects path traversal:
  refuses any input containing `..` outright, then normalizes
  remaining paths with `os.path.normpath` before the allow-list
  check. Previously `/dev/lab/../etc/passwd` slipped through.
- `_audit_actuator_event` / `_audit_raw_actuator_set` / new
  `_audit_safe_drive` wrap their emit call in try/except so a
  bookkeeping failure can't fail a verb that physically succeeded.
- `_admin_actuator_ops_allowed` now logs at debug on config-load
  errors so silent gate-disables are diagnosable.
- `pre_power_apply` audit details now include `cycle_changed` so a
  paired `actuate` → `pre_power_apply` row pair (the normal
  `enter_recovery` shape) reads as "operator drove the strap, then
  the power-flow rechecked it" rather than as a double-toggle.

### Fixed
- Kasa multi-outlet strips no longer hit themselves with parallel KLAP
  handshakes during monitor cycles. Previously each outlet of an N-outlet
  strip opened its own KLAP session in parallel, so the strip
  rate-limited bursts of N concurrent auth attempts as
  `AuthenticationError`. Symptom in production logs: every monitor cycle
  produced a wave of "challenge mismatch" errors against `192.168.4.140`
  (a 6-outlet HS300) followed by an exhausted-retry ERROR per outlet,
  then a fresh wave 60 s later. Retries never helped because the cause
  was contention, not transience. Fix: a process-wide per-host state
  cache (5 s TTL) with a per-host serialization lock. The first caller
  in a cycle pays one KLAP handshake and populates is-on for every
  outlet; concurrent callers on sibling outlets read from the cache.
  Writes invalidate so the next read reflects the change.

### Added
- `labctl status` now appends the last 5 audit-log events under the SBC
  table for at-a-glance recent-action context (oldest first so the newest
  sits closest to the prompt).
- Per-SBC `power_cycle_delay_seconds` (schema v7). Acts as both the
  default when `labctl power cycle --delay` is omitted *and* the floor
  when a smaller value is passed (the CLI raises it with a warning).
  Set/clear with `labctl edit <name> --cycle-delay <seconds>`
  (negative value clears the override). Global fallback default is 3 s.

### Changed
- Kasa retry path now logs at WARNING for each retry attempt (was DEBUG,
  invisible at default log levels), logs at INFO when a retry succeeds,
  and logs at ERROR on final failure. Default retry count bumped from 1
  to 2 (3 total attempts) to absorb intermittent KLAP auth flakes.
- `labctl power cycle --delay` is now optional. Default and floor are
  resolved per-SBC: SBC value if set, otherwise 3 s. Help text corrected
  (was "default: 2s", code default was 2.0 — now 3.0).
- Monitor daemon now runs two cadences (2026-04-30):
  - **Fast track (`health.check_interval`, default 10 s)** — ping +
    serial probes, parallelized across SBCs via a thread pool. Sets
    `sbcs.status` more responsively.
  - **Slow track (`health.power_check_interval`, default 60 s)** —
    power probes, also parallelized. Stays at 60 s by default to limit
    pressure on plug controllers (Kasa KLAP especially).
  - **`health.min_sleep_seconds` (default 1.0)** — floor on the
    between-cycle sleep so a runaway cycle (elapsed ≥ interval) can't
    pin a CPU spinning.
  - **Default `check_interval` 60 s → 10 s** for fresh installs;
    existing configs keep their explicit value. Set
    `power_check_interval: 10` in config to also tighten power
    polling once Phase 2 lands (Kasa broadcast discovery).
- `HealthChecker.check_all` is now parallel (ThreadPoolExecutor,
  max 16 workers). Wall time is the *max* of per-SBC probe latencies
  rather than the sum. Per-SBC failures isolate; one bad probe
  returns an empty summary instead of dropping the row.
- `labctl status --fast` staleness threshold derives from
  `2 × power_check_interval` (was hardcoded 120 s) so it adapts when
  you change the cadence.

### Added
- `labctl status --fast` (alias `-f`) renders from the daemon's cached
  power observation instead of probing every plug live. Boot times go
  from "stuck on the slowest plug" to instant: 6.6s → 0.15s on a 6-SBC
  lab. Stale observations (older than 2× the monitor interval, default
  120s) render dimmed so it's obvious when the daemon is behind. The
  default mode still does live probing — `--fast` is opt-in. Schema v6
  adds `last_power_state` and `last_power_at` columns to `sbcs`; the
  monitor daemon UPDATEs them every cycle. 11 new tests cover the
  schema migration, manager observation API, and `--fast` rendering
  including staleness dimming and the no-observation-yet case
  (2026-04-27)

### Fixed
- `labctl connect` now puts the local TTY in raw mode for the duration
  of the session, so each keystroke is forwarded immediately. Previously
  it shelled out to `nc`/`telnet`, which left the local terminal in
  cooked/line-buffered mode — boot menus with ~3s timeouts (Jetson
  U-Boot, Raspberry Pi, etc.) silently dropped keystrokes because they
  sat in the kernel until the user hit Enter. Native loop with select()
  + tty.setraw + TCP_NODELAY; non-TTY stdin/stdout still falls back to
  `nc`. New escape sequence: Ctrl+] then 'q' (or Ctrl+\) to disconnect,
  Ctrl+] then Ctrl+] to send a literal Ctrl+]. 12 regression tests
  cover the escape state machine and TTY/non-TTY dispatch (2026-04-26)

- `labctl serial list` showed registered adapters as "unassigned" when
  their assignment rows had `serial_device_id = NULL`. Root cause:
  `ResourceManager.assign_serial_port()` only populated the FK when
  the caller explicitly supplied `serial_device_id`. Callers that
  passed only a `/dev/lab/<name>` path (MCP `assign_serial_port`,
  REST API, and CLI without `--serial-device`) left the FK NULL, so
  the two-tier adapter→port link was broken while the port itself
  kept working at the device layer.
  Fix: `assign_serial_port()` now auto-resolves the FK from
  `device_path` (expects `/dev/lab/<name>` or a bare name). New
  `labctl serial repair [--apply]` command backfills the FK for
  existing orphan rows (dry-run by default). 6 new regression tests
  cover the auto-resolve and repair paths (2026-04-19)

### Added
- Activity stream Phase D — polish (2026-04-20)
  - MCP resources `lab://activity/recent` and
    `lab://activity/{sbc_name}` now expose the most recent activity
    events as JSON
  - `labctl activity export --format ndjson` exports activity events
    for downstream processing
  - The existing claim-sweep worker now also prunes activity events
    older than 30 days
  - Regression coverage added for MCP resources, CLI export, and
    activity retention pruning
- Activity stream Phase C — source fidelity (2026-04-20)
  - MCP mutating tools now run inside `activity_context()` so audit
    events record `source=mcp` and the active MCP session ID as `actor`
    instead of falling back to `internal`
  - Flask request lifecycle now sets per-request audit attribution for
    authenticated REST API and web dashboard mutations (`api:<user>` /
    `web:<user>`)
  - Web claim endpoints now derive the claimant name from the correct
    session key (`session["user"]`)
  - Regression tests added for MCP, API-key, and logged-in web
    mutation attribution
- Activity stream Phase B — live (2026-04-19)
  - `ActivityBroadcaster` background thread polls `audit_log` every
    500ms and fans new rows to per-subscriber queues. Cross-process:
    events from CLI/MCP/daemon reach browsers because everyone writes
    to the same DB.
  - `GET /activity` — web page with live feed, source/result filter
    chips, pause/resume, and server-rendered hydration of the last
    200 events.
  - `GET /activity/stream` — Server-Sent Events endpoint, session-
    authed. 15s keepalive heartbeats.
  - `GET /api/activity` — JSON query endpoint (API-key auth), same
    filters as the CLI.
  - `labctl activity tail --follow` — streams live events by polling
    the DB directly (works when the web service is stopped).
  - Activity link added to the nav bar.
  - 9 broadcaster tests (no-replay semantics, fan-out, unsubscribe,
    SSE frame format, slow-consumer drain, hydration + live).
- `labctl services status` — reports on systemd units labctl depends
  on: `labctl-monitor`, `labctl-mcp`, `labctl-web`, and `ser2net`.
  Shows ActiveState/SubState, uptime, restart counter, and recent
  error-level journal lines for any unit that is unhealthy or has
  restarted. Exits non-zero if anything is not running. Extend the
  list with `--unit`. Non-privileged (uses `systemctl show` and
  `journalctl -u`) (2026-04-19)
- Activity stream Phase A — foundation (2026-04-19)
  - `labctl.core.audit` module: `emit()`, `activity_context()`,
    `set_context()`, redaction, millisecond timestamps
  - Schema v5: extends `audit_log` with `actor`, `source`, `result`,
    `claim_id` columns + indexes
  - CLI boundary sets actor/source context in `main()`
  - `labctl activity tail` queries the stream with
    `--sbc/--actor/--source/--result/--since/-n` filters
  - Power CLI commands emit activity events on success/failure
  - Existing manager `_audit_log` calls route through `audit.emit()`
  - 18 tests: schema migration, emit, contextvars, redaction, truncation
  - Spec: `docs/RESOLVED_SPEC_activity_stream.md`


- Hardware claims polish, Phase E (2026-04-16)
  - `ClaimsConfig.validate()` clamps invalid bounds at load time
  - `prune_released_claims()` auto-deletes old released claims (wired
    into CLI expire + MCP sweep)
  - `get_claim_metrics()` aggregate stats — exposed via `labctl claims stats`
    and `lab://claims/metrics` MCP resource
  - `AGENT_RULES.md` section 11: claim workflow, naming, duration guide
- Hardware claims operator tooling, Phase D (2026-04-16)
  - Web REST API: full claim lifecycle at `/api/claims/*`
  - Dashboard: claim badges on SBC cards (agent, time remaining, request warning)
  - SBC detail page: claim section with holder info, pending requests,
    and force-release button
  - MCP tool responses: `_claim_advisory()` appends pending release request
    notices to all 10 gated tools on success
- Hardware claims expiry and dead-session handling, Phase C (2026-04-16)
  - MCP `atexit` handler: releases this session's claims on clean exit
  - `release_dead_sessions()`: checks `kill -0 <pid>` for mcp-stdio claims;
    dead process + expired grace → auto-release as `session-lost`
  - Background daemon thread in MCP server sweeps every 30s
  - `labctl claims expire` CLI command for cron-driven sweeps
  - Grace period respected: dead sessions within grace window are left alone
- Hardware claims MCP integration, Phase B (2026-04-16)
  - MCP tools: `claim_sbc`, `release_sbc`, `renew_sbc_claim`, `list_claims`,
    `get_claim`, `request_sbc_release`, `force_release_sbc`
  - MCP resources: `lab://claims`, `lab://claims/{sbc_name}`,
    `lab://claims/history/{sbc_name}`
  - Claim enforcement on 10 mutating MCP tools — other-agent claims
    return structured `sbc_claimed` JSON errors with hints
  - Session identity via `mcp-stdio:<pid>-<start_epoch>` for stdio transport
  - Heartbeat fires on every claimant tool call (reads and writes)
- Hardware claims (exclusive access coordination), Phase A (2026-04-16)
  - SQLite schema v4 adds `claims` and `claim_requests` tables
  - Partial unique index enforces at most one active claim per SBC
  - CLI: `labctl claim`, `release`, `renew`, `force-release`, `request-release`
  - CLI: `labctl claims list | show <sbc> | history <sbc>`
  - `labctl status` surfaces the current claim holder and pending release requests
  - `labctl remove <sbc>` refuses while a claim is active (use `--force` to override)
  - Manager ops: `claim_sbc`, `release_claim`, `renew_claim`, `heartbeat_claim`,
    `get_active_claim`, `list_active_claims`, `list_claim_history`,
    `force_release_claim`, `expire_stale_claims`, `record_release_request`
  - Structured exceptions: `ClaimConflict`, `ClaimNotFoundError`, `NotClaimantError`,
    `UnknownSBCError`
  - Acquisition runs expire-stale sweep inside the transaction so past-deadline
    claims don't block new acquisitions even when no sweeper has run
  - Spec: `docs/RESOLVED_SPEC_claims.md`
- SDWire power safety interlock (2026-04-09)
  - `sdwire_to_host` rejects if SBC is powered on (prevents SD card bus contention)
  - `--force` flag (CLI) / `force` parameter (MCP) to override when SBC is halted but power on
  - `sdwire_update` and `flash_image` auto power-off SBC before switching to host
  - Best-effort: allows operation when power state is unknown
- `flash_image` MCP tool and enhanced `labctl sdwire flash` CLI (2026-04-06)
  - Supports .img, .img.xz, .img.gz compressed formats
  - Safety checks: block device size validation (max 256 GB), mount check, image format validation
  - Post-flash file copies to boot partition (e.g., custom config.txt)
  - On flash failure, SD card left on host for inspection
  - Automatic power-off before flash, optional reboot after
- MCP CRUD tools for full device lifecycle management (2026-04-04)
  - Remove tools: `remove_serial_port`, `remove_network_address`, `remove_power_plug`
  - Serial device tools: `add_serial_device`, `remove_serial_device`, `serial_discover`
  - SDWire tools: `sdwire_add`, `sdwire_remove`, `sdwire_assign`, `sdwire_unassign`, `sdwire_discover`
  - AI assistants can now fully manage lab resources without CLI
- `/deploy-and-test` Claude Code skill (2026-04-02)
  - Orchestrates build, deploy, boot, capture, and analysis via MCP tools
  - Auto-discovers config from `.claude/deploy-config.json`, `CLAUDE.md`, or user prompts
  - Boot reliability testing mode with `--runs N`
  - Located at `skills/deploy-and-test.md`, opt in via project settings.json
- Serial I/O and boot test tools (2026-04-02)
  - `labctl serial capture <port> --timeout N --until <pattern>` — capture serial output with timeout or pattern match
  - `labctl serial send <port> <data>` — send data to serial port, optional `--capture` for response
  - `labctl boot-test <sbc>` — automated boot reliability testing: deploy, reboot N times, report success rate
  - MCP tools: `serial_capture`, `serial_send`, `boot_test`
  - Port name resolution: accepts port alias or SBC name (defaults to console port)
  - 33 new tests for serial capture, send, and boot test
- SDWire SD card multiplexer support (2026-03-31)
  - Supports SDWire, SDWireC, and SDWire3 devices via `sdwire` Python library
  - `labctl sdwire` CLI command group: discover, add, remove, list, assign, unassign, dut, host, flash, update
  - `labctl sdwire flash <sbc> <image>` — automated workflow: switch to host, dd image, switch to DUT, power cycle
  - `labctl sdwire update <sbc> -p N -c src:dest -r old:new -d file` — atomic partition operations: copy, rename, delete files, optional reboot
  - Destination paths relative to partition root (e.g., `kernel.img` not `/boot/firmware/kernel.img`)
  - Database schema v3: `sdwire_devices` and `sdwire_assignments` tables
  - SDWire shown in `labctl info` output
  - MCP resource `lab://sdwire-devices` and tools `sdwire_to_dut`, `sdwire_to_host`, `sdwire_update`
  - Export/import includes SDWire device assignments
  - Sudoers-based privilege escalation for SD card mount/umount/dd/sync operations (enables MCP server to manage SD cards without running as root)
  - 55 new tests (322 total)
- Global `--delay` / `-d` CLI option (2026-03-29)
  - Adds a delay in seconds before any command executes
  - Useful for chaining commands after power cycles (e.g., `labctl -d 30 health-check`)
  - Suppressed with `--quiet`, prints "Waiting Xs..." otherwise
- MCP (Model Context Protocol) server for AI integration (2026-03-28)
  - `labctl mcp` command — stdio (default) and HTTP transports
  - 7 resources: lab://sbcs, lab://sbcs/{name}, lab://power/{name},
    lab://serial-devices, lab://ports, lab://health/{name}, lab://status
  - 10 tools: power_on/off/cycle, run_health_check, add/remove/update_sbc,
    assign_serial_port, assign_power_plug, set_network_address
  - 2 prompts: debug-sbc (guided debugging), lab-report (status overview)
  - Uses official `mcp` Python SDK with FastMCP
  - 28 comprehensive tests for tools and resources
- Two-tier serial device management (2026-03-28)
  - `serial_devices` database table for registering physical USB-serial adapters
  - `labctl serial` CLI command group: discover, add, remove, list, rename, udev
  - Port assignments now support `--alias` for human-friendly names and `--serial-device` to link to registered adapters
  - `labctl connect` resolves by alias, SBC name, or device path
  - `labctl serial udev --install --reload` generates udev rules from database (replaces manual YAML editing)
  - Pure-Python USB discovery via `/sys` and `udevadm` (no external script dependency)
  - Database schema v2 migration (adds serial_devices table, alias/serial_device_id columns to serial_ports)
  - Export/import includes serial devices and port aliases
  - Web UI updated: alias field in port assignment form, alias column in port table
  - REST API updated: serial device endpoints, alias in port endpoints
  - Udev rules file group-writable by labctl group (no sudo needed for install)
  - Sudoers entries for passwordless udevadm and ser2net restart
- Kasa power retry logic (2026-03-28)
  - Retries up to 2 times on transient KLAP authentication failures
  - Handles intermittent HS300 firmware handshake errors transparently
- Native HTTPS support for web server (2026-03-28)
  - `--cert` and `--key` CLI flags on `labctl web` command
  - `web:` config section for persistent SSL configuration (cert_file, key_file)
  - Documentation for self-signed certificate generation and setup
  - Systemd service updated with SSL cert read access and port 443 capability
- Kasa Smart Power Strip support (2026-03-28)
  - Auto-detects plugs vs strips via `kasa.Discover.discover_single()`
  - Multi-outlet control using `plug_index` (1-based) for individual outlets
  - TP-Link cloud credential support via `kasa:` config section (required for KLAP devices)
  - Proper async cleanup (`device.disconnect()`) to prevent unclosed session warnings
  - Detailed logging at all levels (DEBUG/INFO/ERROR) for troubleshooting
- SBC renaming support (2026-03-28)
  - `labctl edit <name> --rename <new-name>` CLI command
  - Rename via REST API (`PUT /api/sbcs/<name>` with `{"name": "new-name"}`)
  - Rename via web UI edit form
- CLI logging initialization (2026-03-28)
  - `logging.basicConfig()` wired to config `log_level` in CLI entrypoint
  - `-v` flag sets DEBUG, `-q` flag sets WARNING
  - Log output goes to stderr to avoid interfering with command output
- `scripts/kasa-debug.py` — standalone debug script for Kasa device troubleshooting
  - Device discovery, status, and power control
  - `test-auth` action with credential hash diagnostics
  - Verbose mode includes python-kasa internal debug output

### Changed
- Default log level changed from INFO to WARNING (2026-03-28)
- Kasa controller logging lowered from INFO/ERROR to DEBUG (2026-03-28)
  - Prevents log noise in `labctl status` output
- `labctl ser2net generate` uses port alias as connection name when available (2026-03-28)
- `labctl ser2net reload` auto-escalates to sudo when needed (2026-03-28)

### Fixed
- Critical: `get_status_history()` and `get_uptime()` called nonexistent `execute_query()` (2026-04-03)
  - Replaced with `execute()`, fixed tuple indexing to dict-style access for `sqlite3.Row`
- Critical: Open redirect in login — `next` parameter now rejects absolute and protocol-relative URLs (2026-04-03)
- Path traversal protection in SDWire file operations (2026-04-03)
  - Copy, rename, and delete paths validated via `realpath` to stay under mount point
- Null checks after `execute_one()` on 5 insert-then-retrieve sites in manager (2026-04-03)
- Power controller error handling improvements (2026-04-03)
  - Added warning-level logging to Tasmota/Shelly bare exception handlers
  - Added timeout to Kasa `future.result()` to prevent indefinite blocking
  - Fixed missing space in Kasa error message formatting
- SerialProxy thread safety fixes (2026-04-03)
  - `asyncio.Lock` for write lock acquisition to prevent TOCTOU race
  - Snapshot `clients` dict before broadcast iteration
  - Guard orphaned `create_task(self.stop())` with state check
- ser2net generate: add /dev/lab/ prefix for bare device names (2026-04-09)
- ser2net format string: revert to `<baud><parity><databits><stopbits>` (e.g. `115200n81`) — ser2net rejects the comma-separated form with "unknown parameter 8n1" (2026-04-16)
- Monitor daemon: `AlertManager.trigger()` call on power state change used kwargs instead of a single `Alert` object, raising `TypeError` each cycle. The uncaught exception aborted `run_once()` mid-iteration, preventing DB status updates for any SBCs processed after the crashing one. Added regression tests for `MonitorDaemon` (2026-04-17)
- Consolidated SBC.to_dict() serialization, eliminating duplication between MCP and API (2026-04-03)
- Atomic upsert (INSERT...ON CONFLICT) for serial ports, network addresses, power plugs, SDWire assignments (2026-04-03)
- Config load exception now logs warning instead of silently continuing (2026-04-03)
- SessionLogger rotation handles disk errors without leaking file handles (2026-04-03)
- ser2net format string: extracted PARITY_CHARS constant (2026-04-03)
- Extracted `_load_serial_device()` helper, eliminating 3x code duplication in manager (2026-04-03)
- Removed unused `_connection` field from Database class (2026-04-03)
- Simplified redundant `is True`/`is False` checks in Shelly controller (2026-04-03)
- SessionLogger timestamps now include milliseconds to prevent collision (2026-04-03)
- MCP parameter validation: `power_cycle` rejects negative delay, `boot_test` requires runs >= 1 and timeout > 0 (2026-04-03)
- Power control API rate limiting: 5s minimum between power cycles per SBC (HTTP 429) (2026-04-03)
- Health check power probe using wrong function signature (2026-03-28)
  - `get_controller(sbc.power_plug)` → `PowerController.from_plug(sbc.power_plug)`
- Monitor daemon ping failing under systemd sandbox (2026-03-28)
  - Removed `NoNewPrivileges=yes`, added `AmbientCapabilities=CAP_NET_RAW`
- Kasa unclosed aiohttp sessions on authentication failure (2026-03-28)
  - `_get_device()` now disconnects device in all error paths
- USB serial discovery returning root hub path instead of device-specific paths (2026-03-28)
- Authentication system (2026-03-06)
  - Session-based web login with username/password
  - API key authentication via `X-API-Key` header for REST endpoints
  - CSRF protection on all web POST/PUT/DELETE forms
  - Auth disabled by default — existing deployments unaffected
  - `/api/health` remains open without authentication for monitoring tools
  - `AuthConfig` and `UserConfig` dataclasses in config module
  - Login page template with dark theme styling
  - Logout button in navigation bar when logged in
  - `labctl user` CLI command group:
    - `labctl user hash-password` — generate werkzeug password hash
    - `labctl user generate-key` — generate random API key
    - `labctl user add <username>` — interactive user creation with YAML output
    - `labctl user verify <username>` — verify password against config
  - Auth section in `config/labctl.yaml.example`
  - 14 authentication integration tests (188 total)
  - `[tool.setuptools.package-data]` in pyproject.toml for templates and static files
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
- M1.5 Configuration File (2025-12-31)
  - Created `src/labctl/core/config.py` with Config dataclasses
  - YAML config loading from ~/.config/labctl/config.yaml
  - Environment variable overrides (LABCTL_DEV_DIR, LABCTL_BASE_TCP_PORT, etc.)
  - Added `-c/--config` CLI option
  - Added 17 unit tests for config module
  - **Milestone 1 Complete!**
- M2.1-2.5 Data Layer (2025-12-31)
  - Created `src/labctl/core/database.py` with SQLite schema
    - Tables: sbcs, serial_ports, network_addresses, power_plugs, status_log, audit_log
    - Foreign keys with cascade delete
    - Schema versioning support
  - Created `src/labctl/core/models.py` with data models
    - SBC, SerialPort, NetworkAddress, PowerPlug dataclasses
    - Status, PortType, AddressType, PlugType enums
    - from_row() methods for database deserialization
  - Created `src/labctl/core/manager.py` with ResourceManager
    - Full CRUD operations for SBCs
    - Serial port, network address, power plug assignment
    - Audit logging for all operations
  - Added CLI SBC management commands
    - `labctl list` - tabular output with filters (--project, --status)
    - `labctl add <name>` - create SBC with options
    - `labctl remove <name>` - delete with confirmation
    - `labctl info <name>` - detailed SBC view
    - `labctl edit <name>` - update properties
  - Added CLI port assignment commands
    - `labctl port assign <sbc> <type> <device>` - with auto TCP port
    - `labctl port remove <sbc> <type>`
    - `labctl port list` - all port assignments
  - Added 27 new tests (8 database, 19 manager)
  - Total: 62 tests passing
- M2.6-2.7 Network and ser2net Commands (2025-12-31)
  - Added `labctl network set <sbc> <type> <ip>` with --mac, --hostname options
  - Added `labctl network remove <sbc> <type>`
  - Added `labctl ser2net generate` - generates config from database
    - Supports --output file and --install flag
  - Added `labctl ser2net reload` - restarts ser2net service
  - **Milestone 2 Complete!**
- M3 Power Control (2025-12-31)
  - Created power module `src/labctl/power/` with controller framework
    - PowerController ABC with power_on, power_off, power_cycle, get_state
    - PowerState enum (ON, OFF, UNKNOWN)
    - Factory function get_controller() for plug type dispatch
  - Implemented TasmotaController for Tasmota smart plugs
    - HTTP API client with multi-relay support
  - Implemented ShellyController for Shelly devices
  - Implemented KasaController stub for TP-Link Kasa (requires python-kasa)
  - Added CLI plug commands
    - `labctl plug assign <sbc> <type> <address>` with --index option
    - `labctl plug remove <sbc>`
  - Added CLI power commands
    - `labctl power on/off/cycle/status <sbc>`
    - `labctl power-all on|off` with --project filter
  - Added 19 power module tests (81 total)
  - **Milestone 3 Complete!**
- M4 CLI Completion (2025-12-31)
  - Added `labctl console <sbc>` - connect to SBC's serial console by name
    - Looks up port from database, supports --type for jtag/debug
  - Added `labctl ssh <sbc>` - SSH to SBC by name
    - Looks up IP from database, uses configured ssh_user
    - Supports --user override
  - Added `labctl status` - overview with color-coded status and power state
  - Added `labctl export` - export SBCs to YAML/JSON
  - Added `labctl import <file>` - import SBCs with --update option
  - Added `labctl completion` - shell completion for bash/zsh/fish
  - **Milestone 4 Complete!**
- M5 Web Interface (2025-12-31)
  - Created Flask web module with application factory pattern
  - Implemented REST API endpoints:
    - `GET/POST /api/sbcs` - list and create SBCs
    - `GET/PUT/DELETE /api/sbcs/<name>` - CRUD operations
    - `GET/POST /api/sbcs/<name>/power` - power status and control
    - `GET /api/ports` - list serial ports
    - `GET /api/health` and `/api/status` - system status
  - Created web dashboard with dark theme:
    - Dashboard page with SBC card grid
    - SBC detail page with all information
    - Power control buttons (on/off/cycle)
    - Status indicators and responsive layout
  - Added `labctl web` command with --host, --port, --debug options
  - **Milestone 5 Complete!**
- M6 Multi-Client Serial Access (2026-01-01)
  - Created serial proxy module `src/labctl/serial/proxy.py`
    - SerialProxy class with asyncio-based fan-out architecture
    - First-writer-wins write lock policy
    - Multiple readers can view console output simultaneously
    - ProxyManager for managing multiple proxy instances
  - Added session logging
    - SessionLogger class for timestamped traffic logs
    - Logs stored in ~/.local/share/labctl/logs/
    - Format: [timestamp] [direction] data
  - Added ProxyConfig to config.py
    - Configurable port range (default 5000-5099)
    - Write policy selection (first, all, queue)
    - Max clients per proxy, idle timeout
  - Added CLI proxy commands
    - `labctl proxy start <sbc>` - start proxy for SBC (foreground)
    - `labctl proxy list` - list running proxies (placeholder)
    - `labctl sessions [sbc]` - list connected clients (placeholder)
  - Added web console with xterm.js
    - WebSocket bridge for browser-to-proxy communication
    - Console page at /sbc/<name>/console
    - Full terminal emulator in browser
  - Added 26 proxy tests (143 total)
  - **Milestone 6 Complete!**
- M7 Monitoring and Health (2026-01-01)
  - Created health check module `src/labctl/health/`
    - HealthChecker class with ping, serial, and power checks
    - CheckResult and HealthCheckSummary dataclasses
    - Automatic status determination based on check results
  - Added alerting framework
    - Alert and AlertLevel for structured notifications
    - AlertHandler ABC with pluggable handlers
    - LogAlertHandler for file-based alerts
    - ConsoleAlertHandler for terminal output
    - EmailAlertHandler and SlackAlertHandler stubs for future
    - AlertManager for dispatching to multiple handlers
  - Created monitoring daemon
    - MonitorDaemon class for periodic health checks
    - Configurable check interval
    - Automatic status updates on check results
    - Alert triggering on status/power changes
  - Added status tracking
    - log_status() method writes to status_log table
    - get_status_history() for retrieving history
    - cleanup_old_status_logs() for retention management
  - Added HealthConfig to configuration
    - check_interval, ping_timeout, serial_timeout
    - status_retention_days, alert_log_path
    - alert_on_offline, alert_on_power_change options
  - Added CLI commands
    - `labctl health-check` - run health checks on SBCs
      - --type ping|serial|power|all filter
      - --sbc to check single SBC
      - --update to update status in database
    - `labctl monitor` - start monitoring daemon
      - --interval to override check frequency
      - --no-update, --no-alerts options
  - Added web endpoints
    - GET /api/sbcs/<name>/history - status history
    - GET /api/health/check - run health checks via API
    - Status history page at /sbc/<name>/history
  - Added 28 health module tests (171 total)
  - **Milestone 7 Complete!**
- Deferred Items Implementation (2026-01-01)
  - CLI Enhancements:
    - `labctl log <sbc>` - capture serial output to timestamped file
      - `--follow/-f` for continuous output
      - `--lines/-n` to capture N lines then exit
      - `--timestamp/--no-timestamp` for timestamp prefixes
    - `labctl status --watch` - continuous status update display
    - `labctl port list --unassigned` - show unassigned /dev/lab/* devices
    - `-q/--quiet` global flag for suppressed output
    - Command aliases: ls=list, rm=remove, delete=remove, show=info
  - Web Interface:
    - SBC edit form on detail page
    - Port/network/plug assignment forms
    - Settings page with configuration display
    - Settings link in navigation
    - GET /api/sbcs/<name>/console/info endpoint
    - GET /api/sbcs/<name>/uptime endpoint
  - Infrastructure:
    - Systemd service files (labctl-monitor.service, labctl-web.service)
    - Service installation script (scripts/install-services.sh)
    - Session log rotation with size-based rotation and compression
    - Old log cleanup based on retention period
  - Health Monitoring:
    - Uptime tracking with current uptime and 24h percentage
    - Serial probe with string/response verification
    - `serial_probe()` method for active serial testing

### Changed
- Moved documentation files to docs/ folder (AGENT_RULES.md, IMPLEMENTATION.md, DECISIONS.md)
- Fixed README.md and CLAUDE.md paths to reference docs/ folder

### Fixed
- Nothing yet

---

## Version History

_No releases yet. Development starting with Milestone 1._
