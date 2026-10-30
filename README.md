# Embedded Lab Control (labctl)

Centralized management system for embedded development lab resources. Provides deterministic USB serial access, power control, and resource tracking for multiple single-board computers (SBCs).

## Features

- **Deterministic USB Serial Naming** - udev rules create stable `/dev/lab/<sbc>` symlinks
- **Remote Serial Console** - TCP access via ser2net with multi-client support
- **Smart Plug Power Control** - Supports Tasmota, Kasa, and Shelly devices
- **Web Dashboard** - Browser-based monitoring and control
- **REST API** - Programmatic access to all features
- **Health Monitoring** - Automated ping, serial, and power checks
- **Activity Stream** - Unified audit trail with CLI tailing, live web feed, and API queries
- **Hardware Claims** - Exclusive-access coordination for long-running SBC workflows
- **Session Logging** - Capture serial output with rotation and compression
- **Authentication** - Session-based web login and API key auth (optional, disabled by default)
- **Claude Code Skill** - an example `/deploy-and-test` skill orchestrates build, deploy, boot, and serial capture over MCP ([install](#claude-code-skill-deploy-and-test))

## Installation

### Prerequisites

- Linux, Python 3.10+. Some extras need newer Python: Kasa devices that use
  KLAP v2 authentication need 3.11+ (`python-kasa` 0.12), and the `sdwire`
  extra installs nothing below 3.12 (SDWire commands then report the package
  as missing).

```bash
# System packages
sudo apt install python3-venv ser2net

# Clone repository
git clone https://github.com/johnjezl/Embedded-Lab-Control.git
cd Embedded-Lab-Control
```

### Setup

labctl is not on PyPI yet, so install it from a checkout:

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with the extras you need: web (dashboard/REST), mcp (AI
# integration), kasa (TP-Link plugs), sdwire (SD muxes), actuators (USB relays)
pip install -e ".[web,mcp,kasa,sdwire,actuators]"

# Copy example config (a personal config; see "Config file search order")
mkdir -p ~/.config/labctl
cp config/labctl.yaml.example ~/.config/labctl/config.yaml
```

Once the package is published, `pip install 'embedded-lab-control[web,mcp]'`
(the distribution name; the command is still `labctl`) will work as well.

Deterministic `/dev/lab/<name>` serial names come from udev rules generated
from the adapters you register:

```bash
labctl serial discover                 # find adapters and their USB paths
labctl serial add sbc1-console 1-10.1.3
labctl serial udev --install --reload  # write /etc/udev/rules.d/99-lab-serial.rules
```

For a shared, system-wide install with systemd services, see
[Systemd Services](#systemd-services).

### Config file search order

labctl uses the first config file it finds:

1. `-c/--config PATH` (only that file)
2. `$LABCTL_CONFIG`
3. `~/.config/labctl/config.yaml` (or `$XDG_CONFIG_HOME/labctl/config.yaml`)
4. `/etc/labctl/config.yaml`
5. Built-in defaults

> **Warning:** a personal `~/.config/labctl/config.yaml` wins over the shared
> `/etc/labctl/config.yaml`. On a system install, a personal config left over
> from the Setup step silently points labctl at a different database than the
> services use. Remove it, or pass `-c /etc/labctl/config.yaml`.

## Quick Start

```bash
# Add an SBC
labctl add rpi4 --description "Raspberry Pi 4"

# Assign its console serial port (type: console, jtag or debug)
labctl port assign rpi4 console /dev/lab/rpi4 --baud 115200

# Assign a power plug (Tasmota example)
labctl plug assign rpi4 tasmota 192.168.1.100

# View status
labctl status

# Connect to serial console
labctl console rpi4

# Control power
labctl power on rpi4
labctl power cycle rpi4 --delay 5

# Start web dashboard
labctl web --port 5000
```

## CLI Reference

Every command has `--help`. This section lists the commands; see
[docs/OPERATIONS.md](docs/OPERATIONS.md) for what each one changes.

### Global Options

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose (DEBUG) output |
| `-q`, `--quiet` | Suppress non-essential output |
| `-d`, `--delay <seconds>` | Wait before executing the command (put it before the command name) |
| `-c`, `--config <path>` | Use this config file only (see [search order](#config-file-search-order)) |
| `--version` | Show the version and exit |

### Common Flags

| Flag | Description |
|------|-------------|
| `-y`, `--yes` | Skip the confirmation on destructive commands. The question is asked only when stdin is a terminal, so cron, CI, pipes and agents are never prompted. `remove`, `power-all`, `sdwire remove`, `serial remove` and `actuator remove` ask whenever `-y` is missing, so pass `-y` in scripts. |
| `--override-claim` | Act on a board another user has claimed (or that shares hardware with one). The override is recorded in the activity log. Without it, such commands refuse. |
| `--dry-run` | On `power cycle`, `sdwire flash` and `sdwire update`: print the plan and change nothing |

### SBC Management

| Command | Description |
|---------|-------------|
| `labctl add <name>` | Add a new SBC |
| `labctl remove <name>` | Remove an SBC |
| `labctl list` | List all SBCs |
| `labctl info <name>` | Show SBC details |
| `labctl edit <name>` | Edit SBC properties (--rename, --project, etc.) |
| `labctl edit <name> --cycle-delay N` | Per-SBC off→on delay in seconds; the default and minimum for `power cycle` (negative clears it) |
| `labctl network set <sbc> ethernet\|wifi <ip>` | Set an SBC's IP address (`-m` MAC, `-h` hostname); `network remove` deletes it |
| `labctl ssh <sbc>` | SSH to the SBC's configured IP (`-u` user) |
| `labctl status` | Show status overview |
| `labctl status --watch` | Continuous status monitoring |
| `labctl status --fast` | Skip live power probes; show the monitor daemon's cached power state |
| `labctl export [-f yaml\|json] [-o file]` | Export SBCs with their ports, network addresses, plugs and SDWires, plus serial adapters |
| `labctl import <file> [--update]` | Import an export file (skips existing SBCs unless `--update`) |

### Activity Stream

| Command | Description |
|---------|-------------|
| `labctl activity tail` | Show recent activity events |
| `labctl activity tail --follow` | Stream new activity events by polling the database |
| `labctl activity tail --actor <actor>` | Filter events by actor |
| `labctl activity tail --source <source>` | Filter events by source (`cli`, `mcp`, `api`, `web`, `daemon`, `internal`) |
| `labctl activity tail --result <result>` | Filter events by result (`ok`, `error`, `forbidden`) |
| `labctl activity tail --sbc <name>` | Filter events for one SBC |
| `labctl activity export --format ndjson` | Export activity events as NDJSON |

### Claims (Exclusive Access Coordination)

| Command | Description |
|---------|-------------|
| `labctl claim <sbc> -r "<reason>" [-d 2h]` | Claim exclusive access to an SBC (`--reason` is required) |
| `labctl release <sbc>` | Release your active claim |
| `labctl renew <sbc>` | Extend your active claim |
| `labctl force-release <sbc> -r "<reason>"` | Override: release someone else's claim |
| `labctl request-release <sbc> -r "<reason>"` | Politely ask the claimant to release |
| `labctl claims list` | List all active claims |
| `labctl claims show <sbc>` | Show the active claim on one SBC |
| `labctl claims history <sbc>` | Show released claim history for one SBC |
| `labctl claims expire` | Run one expiry/dead-session sweep (also prunes old released claims) |
| `labctl claims stats` | Show aggregate claim statistics |

### SDWire (SD Card Multiplexer)

| Command | Description |
|---------|-------------|
| `labctl sdwire discover` | Scan for connected SDWire devices |
| `labctl sdwire add <name> <serial>` | Register an SDWire device |
| `labctl sdwire remove <name>` | Unregister an SDWire device |
| `labctl sdwire list` | List all registered SDWire devices |
| `labctl sdwire assign <sbc> <device>` | Assign SDWire to an SBC |
| `labctl sdwire unassign <sbc>` | Remove SDWire assignment |
| `labctl sdwire dut <sbc>` | Switch SD card to SBC (boot from SD) |
| `labctl sdwire host <sbc>` | Switch SD card to host (for flashing) |
| `labctl sdwire flash <sbc> <image>` | Power off, flash full image to SD card, power-cycle (`--no-reboot` leaves the SBC off) |
| `labctl sdwire update <sbc> -p N -c src:dest -r old:new -d file` | Power off, then copy, rename, or delete files on a partition (`--reboot` to power-cycle; without it the SBC is left off) |
| `labctl sdwire ls <sbc> -p N [--path /] [--recursive]` | List directory contents on a partition |
| `labctl sdwire cat <sbc> -p N --path /file [--encoding text|base64|hex]` | Read a file from a partition |
| `labctl sdwire info <sbc>` | Show partition table and filesystem metadata |

Partition numbers are 1-based (e.g., `-p 1` for the first partition, which maps to `/dev/sdb1`).
`ls`, `cat` and `info` borrow the card: they switch it to the host and back,
and refuse while the SBC is powered on.

SDWire operations shell out (via `sudo`) to `mount`, `umount`, `dd`, `sync`,
`partprobe`, `parted` and `blkid`. They do not use `sd-mux-ctrl`, and they do
not require a fixed privileged mountpoint such as `/mnt/sdwire`.

To reduce friction for interactive use and MCP/service flows, add a narrowly
scoped sudoers rule. Use its own file: `scripts/install-services.sh` rewrites
`/etc/sudoers.d/labctl`.
```bash
echo '<user> ALL=(root) NOPASSWD: /usr/bin/mount, /usr/bin/umount, /usr/bin/dd, /bin/dd, /usr/bin/sync, /bin/sync, /usr/sbin/partprobe, /sbin/partprobe, /usr/sbin/parted, /sbin/parted, /usr/sbin/blkid, /sbin/blkid' | sudo tee /etc/sudoers.d/labctl-sdwire
sudo chmod 440 /etc/sudoers.d/labctl-sdwire
```

Keep this scoped to the exact commands above. `NOPASSWD` sudo is still privileged;
the point here is to allow SDWire workflows without granting broader root access.

### Serial Device Management

| Command | Description |
|---------|-------------|
| `labctl serial discover` | Scan for connected USB-serial adapters |
| `labctl serial add <name> <usb_path>` | Register a USB-serial adapter |
| `labctl serial remove <name>` | Unregister an adapter |
| `labctl serial list` | List all registered adapters |
| `labctl serial rename <name> <new>` | Rename an adapter |
| `labctl serial udev --install --reload` | Generate and apply udev rules |
| `labctl serial repair [--apply]` | Re-link port assignments to registered adapters (dry run by default) |
| `labctl serial capture <port> --timeout N --until <pattern>` | Capture serial output |
| `labctl serial send <port> <data> --capture N` | Send data (plus CRLF unless `--raw`) to a serial port |
| `labctl ser2net generate [--install]` | Generate the ser2net config from the database (`--install` writes `/etc/ser2net.yaml`) |
| `labctl ser2net reload` | Restart the ser2net service |

### Boot Testing

| Command | Description |
|---------|-------------|
| `labctl boot-test <sbc> -i <image> -d <dest> -e <pattern> -r N` | Deploy a file to the SD card, then power-cycle N times and count boots that print the pattern |
| `labctl boot-test <sbc> --no-deploy -e <pattern> -r N` | Same, without deploying |

### Serial Console

| Command | Description |
|---------|-------------|
| `labctl port assign <sbc> <type> [device]` | Assign a `console`, `jtag` or `debug` port (`-a/--alias`, `-s/--serial-device`, `-t/--tcp-port`, `-b/--baud`) |
| `labctl port remove <sbc> <type>` | Remove a port assignment |
| `labctl port list` | List port assignments |
| `labctl port list --unassigned` | Show unassigned /dev/lab/* devices |
| `labctl ports` | List `/dev/lab` symlinks and their ser2net TCP ports |
| `labctl connect <alias\|sbc\|device>` | Connect by alias, SBC name, or device |
| `labctl console <sbc>` | Connect to serial console |
| `labctl log <sbc>` | Capture serial output to a log file until Ctrl+C (`-n N` to stop after N lines) |
| `labctl log <sbc> --follow` | Same, also echoing each line to the terminal |

### Power Control

| Command | Description |
|---------|-------------|
| `labctl plug assign <sbc> <type> <addr>` | Assign power plug (--index for strips) |
| `labctl plug remove <sbc>` | Remove the plug assignment |
| `labctl power on <sbc>` | Turn on (sets pre_power relays first; see below) |
| `labctl power off <sbc>` | Turn off |
| `labctl power cycle <sbc> [--delay N] [--dry-run]` | Off, wait, set pre_power relays, on. The delay is 0–86400 s; the default and minimum are the SBC's `--cycle-delay`, otherwise 3 s. |
| `labctl power status <sbc>` | Check power state |
| `labctl power-all on/off` | Control all SBCs (claimed boards are skipped) |

### Relays, Bindings and Recovery

USB relays ("actuators") can drive board straps and buttons: recovery mode,
boot select, reset. An actuator channel is *bound* to an (SBC, purpose) pair.
Only the `lcus1_serial` driver is implemented so far; it needs the
`actuators` extra (pyserial).

| Command | Description |
|---------|-------------|
| `labctl actuator add <name> --driver lcus1_serial --device <path> --channels N` | Register a relay board |
| `labctl actuator list` / `probe <name>` / `remove <name>` | List, check reachability (read-only), or remove (with its bindings) |
| `labctl actuator set <name> <channel> open\|closed` | Drive a channel directly, for checking wiring |
| `labctl bind <sbc> <purpose> --actuator A --channel N --mode latch\|momentary [--phase pre-power]` | Bind a channel (`--pulse-ms` for momentary; `--active-when` for polarity) |
| `labctl unbind <sbc> <purpose>` | Remove a binding |
| `labctl bindings list [--target <sbc>]` / `status <sbc> <purpose>` | Show bindings and their desired vs. last state |
| `labctl bindings actuate\|release <sbc> <purpose>` | Assert or release a latch binding |
| `labctl bindings press <sbc> <purpose>` | Pulse a momentary binding |
| `labctl enter-recovery <sbc>` | Power off, wait, assert the `recovery_mode` binding, power on |
| `labctl exit-recovery <sbc>` | Same, releasing the strap for a normal boot |

**pre_power bindings** (`--phase pre-power`) are straps that must be in place
before the board gets power. Every power-on (`power on`, `power cycle`,
`power-all on`, the reboot after `sdwire flash`/`update`, `boot-test`, MCP and
REST) first checks those relays on the board and on every board sharing its
outlet. If a relay can't be reached, nothing is switched. labctl then drives
each relay to its desired state and only then applies power. If a strap fails
after a cycle's power-off, the boards are left off rather than booting
without it. `--phase post-power` is stored but not yet acted on. When the
monitor daemon starts, it returns unheld relay channels to their default
state and leaves asserted bindings alone.

### Multi-Client Access

Use `labctl proxy start <sbc>` when several people need to watch the same
console. The proxy keeps viewers attached across normal SBC restarts and is
view-only unless you opt in with `--allow-write`. While a proxy is active,
`labctl connect` and `labctl console` refuse direct access for that SBC so
people do not silently bypass the shared session.

| Command | Description |
|---------|-------------|
| `labctl proxy start <sbc>` | Start a shared serial-console proxy for one SBC; reconnects across SBC restarts and is read-only by default (`--allow-write` to enable input, `--exit-on-disconnect` to stop instead) |
| `labctl proxy list` | Explain the current shared-console proxy mode |
| `labctl sessions <sbc>` | Show shared-console clients once proxy daemon mode exists |

### Health Monitoring

| Command | Description |
|---------|-------------|
| `labctl health-check` | Run all health checks |
| `labctl health-check --type ping` | Ping check only |
| `labctl health-check --sbc <name>` | Check single SBC |
| `labctl monitor` | Run the monitoring daemon in the foreground (`-i` interval, `--no-alerts`) |
| `labctl services status` | Show the state of `labctl-monitor`, `labctl-mcp`, `labctl-web` and `ser2net` (exit 1 if any is not running) |

### User Management

| Command | Description |
|---------|-------------|
| `labctl user hash-password` | Generate password hash for config |
| `labctl user generate-key` | Generate random API key |
| `labctl user add <username>` | Interactive user creation with YAML output |
| `labctl user verify <username>` | Verify password against config |

### MCP Server (AI Integration)

| Command | Description |
|---------|-------------|
| `labctl mcp` | Start MCP server (stdio transport) |
| `labctl mcp --http 8080` | Start MCP server (HTTP transport, binds 127.0.0.1) |
| `labctl mcp --http 8080 --host 0.0.0.0` | Listen on other interfaces (requires `auth.enabled` with API keys) |
| `labctl mcp --http 8080 --cert C --key K` | HTTP transport over TLS |
| `labctl mcp [--http 8080 ...] --check` | Run the startup checks and exit: whether clients need API keys, or why the server would refuse to start |

### Shell Completion

```bash
eval "$(labctl completion bash)"   # or zsh; fish: labctl completion fish > ~/.config/fish/completions/labctl.fish
```

### Aliases

For convenience, common aliases are supported:
- `ls` → `list`
- `rm`, `delete` → `remove`
- `show` → `info`
- `on` → `power on`
- `off` → `power off`

## Configuration

Default config location: `~/.config/labctl/config.yaml`

System installs use `/etc/labctl/config.yaml`. That file contains credentials and
is intended to be group-readable by the `labctl` group, not world-readable. Any
user who should run `labctl` against the shared lab inventory must be added to
the `labctl` group and start a new login session afterward.

```yaml
serial:
  dev_dir: /dev/lab          # udev symlink directory
  base_tcp_port: 4000        # ser2net base port
  default_baud: 115200

ser2net:
  config_file: /etc/ser2net.yaml
  enabled: true

# Authentication (disabled by default)
auth:
  enabled: false
  secret_key: "generate-a-random-string"
  session_lifetime_minutes: 480
  users:
    - username: admin
      password_hash: "generate with: labctl user hash-password"
      api_key: "generate with: labctl user generate-key"

# HTTPS (disabled by default)
web:
  cert_file: "/etc/labctl/ssl/cert.pem"
  key_file: "/etc/labctl/ssl/key.pem"

# TP-Link Kasa credentials (for KLAP-authenticated devices)
kasa:
  username: "your-tplink-email@example.com"
  password: "your-tplink-password"

database_path: ~/.config/labctl/labctl.db
log_level: WARNING

# SQLite tuning. WAL journal mode is always on; this controls how long
# a transaction will wait for an exclusive lock before giving up.
database:
  timeout_seconds: 10.0
```

> **Note on backups:** WAL mode creates `-wal` and `-shm` sidecar files
> next to `labctl.db`. Backup scripts should copy all three together,
> or quiesce the daemon and run `sqlite3 labctl.db 'PRAGMA wal_checkpoint(TRUNCATE)'`
> before copying just the main file.

## Web Interface

Start the web server:

```bash
labctl web --port 5000
```

Access at `http://localhost:5000`

Features:
- Dashboard with SBC status overview
- Power control buttons
- Web-based serial console (xterm.js)
- Live `/activity` page backed by Server-Sent Events
- Claim badges on the dashboard and SBC detail pages; changes to a board
  someone else has claimed are refused (operators may override, logged)
- Settings and configuration view
- Optional login authentication (enable `auth.enabled` in config)
- Optional HTTPS with SSL/TLS certificates

### Enabling HTTPS

labctl supports native HTTPS via SSL/TLS certificates. This is recommended when
authentication is enabled, to protect credentials in transit.

**1. Generate a self-signed certificate:**

```bash
sudo mkdir -p /etc/labctl/ssl
sudo openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout /etc/labctl/ssl/key.pem \
  -out /etc/labctl/ssl/cert.pem \
  -days 365 \
  -subj "/CN=labctl"
```

To include additional hostnames or IPs (recommended for avoiding browser warnings
on your local network):

```bash
sudo openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout /etc/labctl/ssl/key.pem \
  -out /etc/labctl/ssl/cert.pem \
  -days 365 \
  -subj "/CN=labctl" \
  -addext "subjectAltName=DNS:labctl,DNS:tarrasque,DNS:tarrasque.local,IP:192.168.1.100"
```

**2a. Use via CLI flags (ad-hoc):**

```bash
labctl web --cert /etc/labctl/ssl/cert.pem --key /etc/labctl/ssl/key.pem
```

**2b. Or configure in config.yaml (persistent, recommended for systemd):**

```yaml
web:
  cert_file: "/etc/labctl/ssl/cert.pem"
  key_file: "/etc/labctl/ssl/key.pem"
```

**3. Set permissions for the systemd service:**

```bash
sudo chown root:labctl /etc/labctl/ssl/key.pem
sudo chmod 640 /etc/labctl/ssl/key.pem
sudo systemctl restart labctl-web
```

Access at `https://localhost:5000`. Your browser will warn about the self-signed
certificate — this is expected for lab use.

## REST API

Base URL: `http://localhost:5000/api` (or `https://` when SSL is configured)

When authentication is enabled, API requests require an `X-API-Key` header:

```bash
curl -H "X-API-Key: your-api-key" https://localhost:5000/api/sbcs
```

For self-signed certificates, use `curl -k` or `--cacert cert.pem` to bypass
certificate verification:

```bash
curl -k -H "X-API-Key: your-api-key" https://localhost:5000/api/sbcs
```

Key activity and claims endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /api/activity` | Query recent activity events with `limit`, `actor`, `source`, `sbc`, `result`, `since`, and `after_id` filters |
| `GET /api/claims` | List all active claims |
| `GET /api/claims/<sbc>` | Show the current claim on one SBC |
| `GET /api/claims/<sbc>/history` | Show released claim history for one SBC |
| `POST /api/claims/<sbc>` | Create a claim |
| `POST /api/claims/<sbc>/renew` | Renew a claim held by the current caller |
| `POST /api/claims/<sbc>/release` | Release a claim held by the current caller |
| `POST /api/claims/<sbc>/force-release` | Operator override for an active claim |
| `POST /api/claims/<sbc>/request-release` | Record a polite release request for the current claimant |

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sbcs` | GET | List all SBCs |
| `/sbcs/<name>` | GET | Get SBC details |
| `/sbcs` | POST | Create SBC |
| `/sbcs/<name>` | PUT | Update SBC |
| `/sbcs/<name>` | DELETE | Remove SBC |
| `/sbcs/<name>/power` | GET | Get power state |
| `/sbcs/<name>/power` | POST | Control power (see below) |
| `/sbcs/<name>/ports` | POST | Assign a serial port |
| `/sbcs/<name>/console/info` | GET | Console connection info (TCP port, baud rate, proxy port) |
| `/sbcs/<name>/history` | GET | Status history |
| `/sbcs/<name>/uptime` | GET | Uptime statistics |
| `/ports` | GET | List all serial port assignments |
| `/status` | GET | Status overview of all SBCs |
| `/health/check` | GET, POST | GET: last health-check results; POST: run a new check |
| `/health` | GET | Service liveness and version (always open, no auth required) |

`POST /sbcs/<name>/power` takes `{"action": "on"|"off"|"cycle"}`, plus an
optional `delay` for a cycle (seconds, 0–86400, default 3.0) and
`"override_claim": true` to act on a board another user has claimed.
Power-on and cycle set the pre_power relays first
(see [Relays, Bindings and Recovery](#relays-bindings-and-recovery)). Responses:

| Status | Meaning |
|--------|---------|
| 200 | Done; the body includes the new `state` |
| 400 | Bad `action` or `delay`, or no plug assigned |
| 403 | An `agent`-role user tried `override_claim` (`not_operator`) |
| 404 | No such SBC |
| 409 | Refused with nothing switched: the board is claimed (`sbc_claimed`) or must be claimed first (`claim_required`), or a pre_power relay couldn't be checked or set before power-on (`{"error": ..., "switched": false}`) |
| 429 | A cycle requested within 5 s of the last one on this board |
| 500 | The plug failed, or a strap failed after a cycle's power-off (the board may be off) |

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for the full claim and REST
error codes.

## MCP Server (AI Integration)

labctl includes an MCP (Model Context Protocol) server for AI assistant integration.
This allows tools like Claude Desktop and Claude Code to manage lab resources directly.

Install the MCP dependency from a checkout (the `[mcp]` extra):

```bash
pip install -e ".[mcp]"
# after publication on PyPI: pip install 'embedded-lab-control[mcp]'
```

**Resources** (read-only data): `lab://sbcs`, `lab://sbcs/{name}`, `lab://power/{name}`,
`lab://serial-devices`, `lab://sdwire-devices`, `lab://ports`, `lab://health/{name}`,
`lab://status`, `lab://claims`, `lab://claims/{name}`, `lab://claims/history/{name}`,
`lab://claims/metrics`, `lab://activity/recent`, `lab://activity/{name}`

**Tools** (actions): `power_on`, `power_off`, `power_cycle`, `run_health_check`,
`add_sbc`, `remove_sbc`, `update_sbc`, `assign_serial_port`, `assign_power_plug`,
`set_network_address`, `sdwire_to_dut`, `sdwire_to_host`, `sdwire_update`,
`flash_image`, `serial_capture`, `serial_send`, `boot_test`, `list_images`,
`delete_image` (and more: see the docs)

**Uploads**: clients upload builds over HTTP to the MCP server
(`PUT /images/<name>`, same API key) and pass the bare name to
`flash_image`, `sdwire_update` or `boot_test`, so an agent can build on
another machine ([Uploading images](docs/MCP_SERVER.md#uploading-images)).

**Prompts**: `debug_sbc` (guided SBC debugging), `lab_report` (comprehensive status)

See `docs/MCP_SERVER.md` for full tool parameters and usage.

**Safety:** destructive tools (power off, flashing, `serial_send`, deletions,
…) act only when called a second time with a confirmation token; host files
are reachable only under directories you allowlist in `mcp.allowed_*_paths`
(none by default); the HTTP transport binds loopback and requires the web
users' API keys (`Authorization: Bearer`) when `auth.enabled` is set. See
[Security Considerations](docs/MCP_SERVER.md#security-considerations).

### Claude Desktop / Claude Code Configuration

For several Claude Code agents sharing the lab (each with its own API key,
so claims keep them apart), see
[Connecting Claude Code agents](docs/CLAUDE_CODE_AGENTS.md).

For a single local client over stdio, add to your MCP client configuration
(reading `/etc/labctl/config.yaml` requires the `labctl` group; otherwise
point `-c` at your own config):

```json
{
  "mcpServers": {
    "labctl": {
      "command": "/opt/labctl/venv/bin/labctl",
      "args": ["-c", "/etc/labctl/config.yaml", "mcp"]
    }
  }
}
```

### Claude Code skill: /deploy-and-test

`skills/deploy-and-test.md` is an example skill that builds, deploys over
SDWire, power-cycles and checks the serial console using the labctl MCP
tools. It is not active in this repository. Claude Code loads a skill from a
`SKILL.md` file, with `name`/`description` frontmatter, inside a directory
named after the skill. To install it for one project (or for all your
projects, under `~/.claude/skills/`):

```bash
mkdir -p <your-project>/.claude/skills/deploy-and-test
cp skills/deploy-and-test.md <your-project>/.claude/skills/deploy-and-test/SKILL.md
```

It expects a connected labctl MCP server, and reads per-target settings from
`.claude/deploy-config.json`.

## Systemd Services

Install labctl system-wide with systemd services:

```bash
sudo ./scripts/install-services.sh

# Optional: MCP server over HTTP on 127.0.0.1:8080
sudo systemctl enable --now labctl-mcp
```

`install-services.sh` creates the `labctl` user and group and installs
labctl with the `web,mcp,kasa,sdwire` extras into `/opt/labctl/venv`, linked as
`/usr/local/bin/labctl`. It sets up `/etc/labctl/config.yaml` (root:labctl,
640) and `/var/lib/labctl`, and makes the udev rules file and
`/etc/ser2net.yaml` writable by the `labctl` group. It writes
`/etc/sudoers.d/labctl` for `udevadm` and `systemctl restart ser2net`.
It enables and starts `labctl-web` and `labctl-monitor`; `labctl-mcp` is
installed but not enabled. For USB relays, also install the `actuators` extra
into the venv (`/opt/labctl/venv/bin/pip install "<checkout>[actuators]"`).

`labctl-mcp` runs `labctl mcp --http 8080`, which binds 127.0.0.1. To
reach it from other machines, add `--host` in a systemd drop-in
(`sudo systemctl edit labctl-mcp`) and enable `auth.enabled` with API keys.

To update an existing install after `git pull` (keeps config and database):

```bash
sudo ./scripts/update.sh
```

## Development

```bash
# Run tests
pytest tests/ -v

# Format code
black src/ tests/
isort src/ tests/

# Lint
flake8 src/ tests/
```

## Documentation

- [Implementation Plan](docs/IMPLEMENTATION.md) - Development roadmap and status
- [Hardware Map](docs/HARDWARE_MAP.md) - USB device identification
- [Decision Log](docs/DECISIONS.md) - Architecture decisions
- [MCP Server](docs/MCP_SERVER.md) - AI integration, security model
- [Claude Code Agents](docs/CLAUDE_CODE_AGENTS.md) - One API key per agent, setup and launch
- [Operations](docs/OPERATIONS.md) - What every command and tool changes
- [Security Policy](SECURITY.md) - Reporting vulnerabilities

## License

MIT
