# MCP Server Documentation

The labctl MCP (Model Context Protocol) server exposes lab management capabilities
to AI assistants like Claude Desktop and Claude Code. It provides resources for
reading lab state, tools for performing actions, and prompts for guided workflows.

## Architecture

The MCP server (`src/labctl/mcp_server.py`) is a thin wrapper around the existing
`ResourceManager`, `PowerController`, and `HealthChecker`. No business logic is
duplicated — all operations go through the same code paths as the CLI and web API.

```
AI Assistant (Claude Desktop / Claude Code)
    │
    │  JSON-RPC over stdio (or HTTP)
    ▼
┌──────────────────────────────┐
│  labctl MCP Server           │
│  (mcp SDK 2.x MCPServer)     │
│                              │
│  Resources ──► read-only     │
│  Tools ──────► mutations     │
│  Prompts ────► workflows     │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  ResourceManager / Power /   │
│  HealthChecker               │
│  (same as CLI + web)         │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  SQLite DB / Kasa / Tasmota  │
│  / Shelly / ser2net / ping   │
└──────────────────────────────┘
```

## Installation

The MCP server requires the `mcp` optional dependency:

```bash
# From a checkout of this repository:
pip install '.[mcp]'

# In the production venv:
sudo /opt/labctl/venv/bin/pip install /path/to/Embedded-Lab-Control[mcp]
```

The package is not on PyPI yet. Once it is published,
`pip install 'embedded-lab-control[mcp]'` works too.

## Starting the Server

### stdio transport (default — for Claude Desktop / Claude Code)

```bash
labctl mcp
```

The server communicates via JSON-RPC over stdin/stdout. This is the standard
transport for local MCP tool integrations.

**Important:** In stdio mode, nothing should be written to stdout except
JSON-RPC messages. All logging goes to stderr.

### HTTP transport (for remote access)

```bash
labctl mcp --http 8080                  # binds 127.0.0.1:8080 (default)
labctl mcp --http 8080 --host 0.0.0.0   # all interfaces — requires auth
labctl mcp --http 8080 --host 0.0.0.0 --cert cert.pem --key key.pem  # HTTPS
```

Uses the Streamable HTTP transport (endpoint `/mcp`) on the specified port.
Useful for multi-client scenarios or accessing the lab from a different machine.
The same port also takes image uploads (`PUT /images/<name>`, see
[Uploading images](#uploading-images)).

#### Authentication

HTTP authentication uses the same users and API keys as the web REST API
(D012): when `auth.enabled` is `true`, every request must carry
`Authorization: Bearer <api_key>` for a user in `auth.users`, otherwise it
gets `401`. Generate a key with `labctl user generate-key` and put it in
that user's `api_key`.

- Keys are re-read from the server's config file on every request, so
  removing or rotating a key takes effect without restarting the server;
  while that file doesn't load (e.g. saved mid-edit), every key is
  refused.
- Each MCP session belongs to the user who opened it; another user's key
  can't drive it, and destructive-tool `confirm_token`s are only valid for
  (and only consumed by) the user they were issued to.
- Each user is a separate claimant: one user's claim blocks another's
  mutating calls, and the default agent name is the username. Claims
  record the username as their **owner**, so the same user's other
  sessions (and the same user after a server restart) count as the
  claimant; audit entries record `mcp-http:<server process>:<username>`.
- Each user has a **role** (`auth.users[].role`): `operator` (the default
  when unset) or `agent`; any other value counts as `agent`, with a
  warning. Only operators may `force_release_sbc`; agents
  get `"error": "not_operator"` and can only `request_sbc_release`.
  The web UI and REST API apply the same roles and owners (see
  [OPERATIONS.md](OPERATIONS.md#web-ui-and-rest-api)).
- The server **refuses to start** without auth on a non-loopback `--host`,
  or when `mcp.allowed_hosts` / `allowed_origins` accept a non-loopback
  name (a reverse proxy). It can't detect a proxy that forwards
  `Host: 127.0.0.1` (nginx's default `proxy_pass` behaviour), so **always
  enable auth before putting a proxy in front**. It also refuses
  `auth.enabled: true` when no user has both an `api_key` and a username
  (quote numeric usernames in YAML).
- It also refuses to start over HTTP if a config file it should read
  can't be loaded (e.g. a YAML typo): running on another file or on
  built-in defaults could silently drop the authentication you enabled.
  That includes `/etc/labctl/config.yaml` being unreadable to you (it is
  `640 root:labctl`): run as a member of the `labctl` group, or pass
  `-c` with a config you can read.
- A running server (either transport) keeps the configuration it started
  with: database, allowlists, confirmation, actuator gate, Kasa and health
  settings. **Restart it to apply config changes** (`scripts/update.sh`
  restarts the services). Only API keys are re-read, from that same file,
  on every request.
- Without auth (the default), it listens on loopback only, all clients
  share one claimant identity, and anyone who can connect locally can call
  every tool. Every HTTP start prints its mode (`MCP HTTP: API keys
  required` / `MCP HTTP: no authentication (loopback only)`).
- `labctl mcp --http PORT [--host ADDR] --check` runs these startup checks
  (the same code the server runs before serving) and exits: it prints
  `auth: required` / `auth: none` (or `n/a` without `--http`) and the
  config file it loaded, plus any startup warnings, whatever `log_level`
  says. It exits with status 1 if the server would refuse to
  start, or if a config file it would read is unusable: unparseable,
  unreadable, or, when named by `LABCTL_CONFIG` (exclusive or not),
  missing. (A `-c FILE` that is missing or unreadable is rejected by the
  CLI itself, exit status 2.) Over HTTP
  the server then refuses to start (falling back could drop the intended
  authentication); over stdio it starts on the next file in the search
  order or on built-in defaults, logging a warning.

#### Host header checks

The server rejects requests whose `Host` header it doesn't expect with
`421 Misdirected Request` (DNS-rebinding protection). Bound to loopback it
accepts `127.0.0.1`, `localhost` and `[::1]` on any port. A reverse proxy
that forwards its public name needs that name added:

```yaml
mcp:
  allowed_hosts: ["lab.example.com", "lab.example.com:*"]  # name:* = any port
  allowed_origins: ["https://lab.example.com"]             # browser clients
```

Accepting a non-loopback name requires auth (see above). On a non-loopback
bind, `Host` and `Origin` are checked only when `allowed_hosts` is set (a
warning is logged otherwise, also noting an `allowed_origins` that is
therefore ignored); API keys are required there regardless.

For remote access, either tunnel (`ssh -L 8080:127.0.0.1:8080 tarrasque`),
put a TLS-terminating reverse proxy in front of the loopback server, or
bind a reachable address with auth enabled and TLS on.

#### TLS

`--cert`/`--key` (or `mcp.cert_file`/`mcp.key_file` in the config; the
flags take precedence) serve HTTPS. Both are needed; the server refuses to
start with only one, with a file it can't read, or with a certificate and
key that don't load as a pair. Without TLS the server
speaks plain HTTP, so API keys and uploads cross the network in clear text,
and a non-loopback bind without TLS logs a warning at startup. The web
server's certificate (`web.cert_file`/`key_file`) can be reused; the
service user (`labctl`) must be able to read both files.

```yaml
mcp:
  cert_file: /etc/labctl/tls/cert.pem
  key_file: /etc/labctl/tls/key.pem
```

Clients then use `https://` URLs. Node-based clients (Claude Code) reject
a self-signed certificate unless they are told to trust it:
`NODE_EXTRA_CA_CERTS=/path/to/cert.pem claude`. `labctl mcp --http PORT
--check` prints the `tls:` and `uploads:` settings it would use.

### Running as a systemd service

A service file is provided for running the MCP server as a persistent HTTP service:

```bash
# Install (included in install-services.sh but not enabled by default)
sudo cp config/systemd/labctl-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable --now labctl-mcp

# Check status
systemctl status labctl-mcp
journalctl -u labctl-mcp -f
```

The service runs on `127.0.0.1:8080` by default. To change the port or add
`--host`, use a drop-in (`sudo systemctl edit labctl-mcp`) that clears and
replaces `ExecStart=`; don't edit the unit file itself, since
`scripts/update.sh` reinstalls it on every update (drop-ins are kept). (Before 0.2.0 the
port argument was ignored and the server silently bound `127.0.0.1:8000`.)

Remote clients connect via HTTP (through a tunnel, a proxy, or `--host`
set to a reachable address), sending their API key when auth is enabled:

```json
{
  "mcpServers": {
    "labctl": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp",
      "headers": { "Authorization": "Bearer <api_key>" }
    }
  }
}
```

### Direct Python invocation

```bash
python -m labctl.mcp_server
```

## Client Configuration

### Claude Code

Over HTTP, with one labctl user and API key per agent so claims keep
agents apart: see [Connecting Claude Code agents](CLAUDE_CODE_AGENTS.md)
(manual `claude mcp add`, and a streamlined per-agent launcher).

For a single local agent over stdio (no key; it runs as you):

```bash
claude mcp add labctl -- /opt/labctl/venv/bin/labctl -c /etc/labctl/config.yaml mcp
```

Reading `/etc/labctl/config.yaml` requires membership in the `labctl`
group; with a config of your own, pass that to `-c` instead.

### Claude Desktop

Add to the Claude Desktop configuration file:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Linux:** `~/.config/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

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

## Resources (Read-Only Data)

Resources provide read-only access to lab state. The AI assistant reads these
to understand the current state before taking action.

### Static Resources

| URI | Description |
|-----|-------------|
| `lab://sbcs` | All SBCs with status, project, IP, serial ports, network, and power plug info |
| `lab://serial-devices` | All registered USB-serial adapters with vendor/model info |
| `lab://ports` | All serial port assignments with aliases, SBC mappings, and TCP ports |
| `lab://sdwire-devices` | All registered SDWire SD card multiplexer devices with assignments |
| `lab://status` | Dashboard-style overview — all SBCs with live power state queries |
| `lab://claims` | All active claims across the lab |
| `lab://claims/metrics` | Aggregate claim statistics (totals by outcome, avg duration) |
| `lab://activity/recent` | Last 50 activity events across the lab |

### Template Resources (parameterized)

| URI | Parameters | Description |
|-----|------------|-------------|
| `lab://sbcs/{sbc_name}` | SBC name | Full details for one SBC |
| `lab://power/{sbc_name}` | SBC name | Live power state query (on/off/unknown) |
| `lab://health/{sbc_name}` | SBC name | Live health check (ping, serial, power) |
| `lab://claims/{sbc_name}` | SBC name | Current claim on an SBC |
| `lab://claims/history/{sbc_name}` | SBC name | Past (released) claims for an SBC (last 20) |
| `lab://activity/{sbc_name}` | SBC name | Last 50 activity events for one SBC |

### Resource Output Format

All resources return JSON. Example for `lab://sbcs/{sbc_name}`:

```json
{
  "name": "jetson-nano-2",
  "project": "SLM-OS",
  "description": "Jetson Nano for ML inference",
  "ssh_user": "root",
  "status": "online",
  "primary_ip": "192.168.4.101",
  "serial_ports": [
    {
      "type": "console",
      "device": "/dev/lab/port-1",
      "alias": "jetson-console",
      "tcp_port": 4000,
      "baud_rate": 115200,
      "serial_device": "port-1"
    }
  ],
  "network_addresses": [
    {
      "type": "ethernet",
      "ip": "192.168.4.101",
      "mac": "00:11:22:33:44:55",
      "hostname": "jetson-nano-2"
    }
  ],
  "power_plug": {
    "type": "kasa",
    "address": "192.168.4.140",
    "index": 1
  }
}
```

## Tools (Actions)

Tools perform mutations — the AI assistant calls these to take action.

### Power Control

| Tool | Parameters | Description |
|------|------------|-------------|
| `power_on` | `sbc_name` | Turn on power to an SBC, after setting its `pre_power` relay bindings |
| `power_off` | `sbc_name` | Turn off power to an SBC |
| `power_cycle` | `sbc_name`, `delay` (seconds, 0-86400; default the board's minimum, see below) | Power cycle (off, wait, set `pre_power` bindings, on); refuses before power-off if a relay can't be opened. Each board's minimum is its stored cycle delay, or 3.0 without one; boards sharing the outlet are cycled too, so the largest of their minimums is the default and the minimum: a smaller `delay` is raised to it, and the result names the board it came from |

Power tools switch the plug outlet, so boards sharing the SBC's outlet are
switched too, and a claim someone else holds on one of them refuses the
call. `power_on` and `power_cycle` also check and set those boards'
`pre_power` relays.

### Health Monitoring

| Tool | Parameters | Description |
|------|------------|-------------|
| `run_health_check` | `sbc_name` (optional) | Run health checks; omit name for all SBCs |

### SBC Management

| Tool | Parameters | Description |
|------|------------|-------------|
| `add_sbc` | `name`, `project`, `description`, `ssh_user` | Create a new SBC record |
| `remove_sbc` | `name` | Delete an SBC and all its assignments |
| `update_sbc` | `name`, `rename`, `project`, `description`, `ssh_user`, `status` | Edit SBC properties |

### Resource Assignment

| Tool | Parameters | Description |
|------|------------|-------------|
| `assign_serial_port` | `sbc_name`, `port_type`, `device`, `alias`, `baud_rate` | Assign serial port to SBC |
| `remove_serial_port` | `sbc_name`, `port_type` | Remove serial port from SBC |
| `assign_power_plug` | `sbc_name`, `plug_type`, `address`, `index` | Assign smart plug to SBC |
| `remove_power_plug` | `sbc_name` | Remove power plug from SBC |
| `set_network_address` | `sbc_name`, `address_type`, `ip_address`, `mac`, `hostname` | Set network address for SBC |
| `remove_network_address` | `sbc_name`, `address_type` | Remove network address from SBC |

### Device Management

| Tool | Parameters | Description |
|------|------------|-------------|
| `add_serial_device` | `name`, `usb_path`, `vendor`, `model`, `serial_number` | Register a USB-serial adapter |
| `remove_serial_device` | `name` | Unregister a USB-serial adapter (refused while a board someone else has claimed has a port on it) |
| `serial_discover` | | Scan for connected USB-serial adapters |

### SDWire (SD Card Multiplexer)

| Tool | Parameters | Description |
|------|------------|-------------|
| `sdwire_to_dut` | `sbc_name` | Switch SD card to SBC (boot from SD) |
| `sdwire_to_host` | `sbc_name`, `force` (default false) | Switch SD card to host (for flashing). Proceeds only when the SBC's plug reports it off: refuses when it reports on, when the SBC has no plug, or when its state can't be read; `force=true` overrides (risks SD card corruption) |
| `sdwire_update` | `sbc_name`, `partition`, `copies`, `renames`, `deletes`, `reboot`, `no_plug_ok` | Copy, rename, or delete files on SD card partition (atomic: power off, mount, operate, unmount, back to DUT). Host `copies` sources must be allowlisted |
| `sdwire_ls` | `sbc_name`, `partition`, `path` (default `/`), `recursive` (default false), `max_entries` (default 1000) | List directory contents on an SD card partition using a read-only mount |
| `sdwire_cat` | `sbc_name`, `partition`, `path`, `max_bytes` (default 1 MiB), `encoding` (`text` default, `base64`, `hex`) | Read a file from an SD card partition with size and encoding guards; symlinks are not followed |
| `sdwire_info` | `sbc_name` | Return partition-table and filesystem metadata for the SD card |
| `sdwire_add` | `name`, `serial_number`, `device_type` | Register an SDWire device |
| `sdwire_remove` | `name` | Unregister an SDWire device |
| `sdwire_assign` | `sbc_name`, `device_name` | Assign SDWire device to SBC |
| `sdwire_unassign` | `sbc_name` | Remove SDWire assignment from SBC |
| `sdwire_discover` | | Scan for connected SDWire devices |
| `flash_image` | `sbc_name`, `image_path`, `reboot`, `post_flash_copies`, `no_plug_ok` | Flash raw disk image (.img/.img.xz/.img.gz) to SD card with safety checks. Host paths must be allowlisted (see [Host file access](#host-file-access-allowlist)) |

`sdwire_update` and `flash_image` power the SBC off first (if it can't
be switched off they refuse and write nothing; a board with no power plug
is refused unless `no_plug_ok=true`, once you've made sure it is off) and
**leave it off** unless `reboot=true`. Their confirmation plan says
whether the board is switched off (`will_do.power_off`):
`sdwire_update` then power-cycles it (waiting the outlet's minimum, as
for `power_cycle`), `flash_image` powers it
on. With `reboot=true` the `pre_power` relays are checked before anything
is switched.

`sdwire_ls`, `sdwire_cat` and `sdwire_info` don't change the card or the
board's power. A card already on the host is read in place and left there.
A card on the DUT is switched to the host and back only when the SBC is
known to be off; otherwise they refuse with `"error": "powered_on"` or
`"power_unknown"` (no power plug, or its state can't be read) and switch
nothing. Once you've made sure such a board is off, `sdwire_to_host(force=true)`
moves the card to the host, and the read tools then read it in place. They return JSON; errors carry `error` and `message`
(e.g. `path_not_found`, `size_limit_exceeded`, `binary_content`: retry with
`encoding="base64"`).

### Images

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_images` | | Images in `mcp.upload_dir` (size, uploader, SHA-256) and how to upload one |
| `delete_image` | `name` | Delete an uploaded image (its uploader or an operator) |

### Serial I/O

| Tool | Parameters | Description |
|------|------------|-------------|
| `serial_capture` | `port_name`, `timeout`, `until_pattern`, `tail` | Capture serial output until timeout or pattern match |
| `serial_send` | `port_name`, `data`, `newline`, `capture_timeout`, `capture_until`, `keys`, `data_hex` | Send data to serial port, optionally capture response. Control characters can't travel in `data` (clients drop them), so name keys in `keys` (`["F11"]`, `["Down", "Enter"]`, `["Ctrl-C"]`; xterm encodings, not yet checked on real firmware; a short pause between keys; function keys differ by terminal type, so firmware that ignores one needs its own sequence as `data_hex`) or give raw bytes as `data_hex`; `newline` defaults to off with either. The confirmation shows the exact bytes (`will_do.bytes_hex`) |

### Boot Testing

| Tool | Parameters | Description |
|------|------------|-------------|
| `boot_test` | `sbc_name`, `expect_pattern`, `runs`, `timeout`, `image`, `dest`, `partition`, `output_dir` | Automated boot reliability testing with deploy and serial capture. `image` is an uploaded image's name or a path in `allowed_read_paths`; `output_dir` must be in `allowed_write_paths` |

### Actuators and Bindings

Actuators are USB relay boards; a binding wires one relay channel to an
SBC for a named purpose (`recovery_mode`, `power_button`, ...). Agents
normally use the binding verbs.

| Tool | Parameters | Description |
|------|------------|-------------|
| `actuator_list` | | List provisioned actuators |
| `actuator_probe` | `name` | Check that an actuator is reachable (records the result on the actuator) |
| `actuator_add` | `name`, `driver` (`lcus1_serial`), `device_path`, `vid`, `pid`, `serial_no`, `channels` (default 1) | Register an actuator. **Privileged**; `device_path` must be under `/dev/lab/`, `/dev/serial/by-id/`, `/dev/ttyUSB*` or `/dev/ttyACM*` |
| `actuator_remove` | `name` | Remove an actuator and the bindings on its channels. **Privileged** |
| `actuator_set` | `name`, `channel` (1-based), `state` (`open`/`closed`) | Drive a channel directly, bypassing bindings (wiring checks only). **Privileged** |
| `bind` | `sbc_name`, `purpose`, `actuator`, `channel`, `mode` (`latch`/`momentary`), `active_when` (`closed` default, or `open`), `pulse_ms` (required for `momentary`), `sample_phase` (`none` default, `pre_power`, `post_power`), `notes` | Bind an actuator channel to (SBC, purpose). `pre_power` bindings are set to their desired state whenever labctl powers the board on |
| `unbind` | `sbc_name`, `purpose` | Remove a binding |
| `bindings_list` | `sbc_name` (optional) | List bindings, for one SBC or all |
| `actuate` | `sbc_name`, `purpose` | Drive a latch binding to its active state (desired = asserted) |
| `release` | `sbc_name`, `purpose` | Drive a latch binding away from active (desired = released) |
| `press` | `sbc_name`, `purpose` | Pulse a momentary binding for its `pulse_ms` |
| `actuation_status` | `sbc_name`, `purpose` | Desired vs. last state of a binding, with its actuator and channel |

The privileged tools refuse unless `mcp.allow_admin_actuator_ops: true` is
set in the config (default off).

### Recovery

| Tool | Parameters | Description |
|------|------------|-------------|
| `enter_recovery` | `sbc_name` | Probe the `recovery_mode` actuator, then power off, wait, assert the strap, power on. Aborts before any power change if the actuator is unreachable |
| `exit_recovery` | `sbc_name` | The same sequence, releasing the strap (normal boot) |

Both need a `recovery_mode` binding and a power plug on the SBC. The wait
is the same minimum as `power_cycle`'s: the largest stored power-cycle
delay among the boards on the SBC's plug outlet, each counting as 3.0 s if
it has none. The result names the board it came from.

### Claims (Exclusive Access Coordination)

| Tool | Parameters | Description |
|------|------------|-------------|
| `claim_sbc` | `sbc_name`, `duration_minutes` (default 30), `reason` (optional), `agent_name`, `context` | Claim exclusive access to an SBC. The duration must lie within `claims.min_duration_minutes`..`max_duration_minutes`; the MCP default is 30, not `claims.default_duration_minutes` |
| `release_sbc` | `sbc_name` | Release your claim (this session, or your user over authenticated HTTP) |
| `renew_sbc_claim` | `sbc_name`, `duration_minutes` | Extend an active claim's deadline |
| `list_claims` | — | List all active claims across the lab |
| `get_claim` | `sbc_name` | Get the current claim on an SBC |
| `request_sbc_release` | `sbc_name`, `reason` | Politely ask the claimant to release |
| `force_release_sbc` | `sbc_name`, `reason` | Operator override — forcibly release (operators only, D013) |

**Claim enforcement:** every tool that changes a board checks its claim:
operating it (`power_on/off/cycle`, `serial_send`, `serial_capture`
(it holds the console's connection), `sdwire_to_host/dut`,
`sdwire_update`, `sdwire_ls/cat/info`, `flash_image`, `boot_test`,
`actuate`/`release`/`press`, `enter_recovery`/`exit_recovery`) and changing
its configuration (`update_sbc`, `remove_sbc`, `assign_serial_port`,
`assign_power_plug`, `set_network_address`, the matching `remove_*` tools,
`sdwire_assign`/`unassign`, `bind`/`unbind`), plus `actuator_set` and
`actuator_remove` on channels bound to a claimed board, and
`remove_serial_device` of an adapter a claimed board has a port on (by
its link, its registration, or a path resolving to the same device). A claim also
covers the board's hardware: if two boards share a plug outlet, SDWire,
serial device or relay channel, a claim on either refuses other users'
tools that operate the other (the error names the claimed board), and
`claim_sbc` refuses a board whose shared hardware someone else holds.
Configuration tools only change the named board's records, so they check
that board alone; assigning hardware another board already uses checks
that board's claim. Boards match by plug type, address text and outlet
(a row saved with outlet 0 matches every outlet on its address), by
SDWire, by serial device path (symlinks resolved), registered adapter
or ser2net TCP port (a TCP port another board's port uses is refused),
and by relay device path (symlinks resolved) or USB serial number and
channel. Two different addresses for one plug (a hostname and its IP)
are not recognised as the same, nor are relay records known only by
VID/PID. Give a single-outlet Kasa plug outlet 1: it ignores the outlet
number, so another number would switch the same plug without matching.
`actuator_set` drives raw relay channels, so leave
`mcp.allow_admin_actuator_ops` off when agents share the lab. If someone else
holds the claim, a structured JSON error is returned with
`"error": "sbc_claimed"` and hints. The claimant's calls proceed and
heartbeat the claim. A claim stops protecting the board when it expires;
for `claims.grace_period_seconds` after that, only its holder can claim
the board again (but someone may claim a board that shares its hardware
meanwhile, which then blocks the re-claim). Claiming a board you already hold (e.g. from an
earlier session) returns `"status": "already_claimed"` and renews it for
the requested duration (without re-checking boards it shares hardware
with).

With `claims.require_claim: true` (default `false`), the tools that name
a board (all of the above except `actuator_set`/`actuator_remove`) also
refuse on a board nobody has claimed, with `"error": "claim_required"`:
agents must `claim_sbc` first. Only the board named in the call needs the
caller's claim; boards it shares hardware with just must not be claimed
by someone else. A value that isn't a boolean counts as `true` (with a
warning) unless it plainly means off (`"false"`, `"no"`, `"off"`, `0`). Other read-only tools are never refused.

### Tool Return Values

All tools return strings. Success messages are plain text, structured data is
returned as formatted JSON. Errors are prefixed with "Error:" or returned as
structured JSON with an `"error"` field (claim conflicts).

## Prompts (Guided Workflows)

Prompts are reusable instruction templates that guide the AI through multi-step
workflows.

### `debug_sbc`

**Parameters:** `sbc_name`

Guides the assistant through debugging an unresponsive SBC:
1. Check SBC state via `lab://sbcs/{name}` resource
2. Check power via `lab://power/{name}` resource
3. Run health check via `run_health_check` tool
4. Take corrective action based on findings (power cycle, etc.)

### `lab_report`

**Parameters:** (none)

Generates a comprehensive lab status report:
1. Read `lab://status` for all SBCs
2. Read `lab://serial-devices` for USB adapters
3. Read `lab://ports` for port assignments
4. Compile summary with issues and recommendations

## Example Interactions

### Checking lab status

> "What's the status of my lab?"

The assistant reads `lab://status` and reports which SBCs are online, offline,
or in error state, along with power information.

### Debugging an SBC

> "My Jetson Nano isn't responding"

Using the `debug_sbc` prompt, the assistant:
1. Reads `lab://sbcs/jetson-nano-2` — sees status is "offline"
2. Reads `lab://power/jetson-nano-2` — sees power is ON
3. Calls `run_health_check(sbc_name="jetson-nano-2")` — ping fails
4. Calls `power_cycle(sbc_name="jetson-nano-2")` — reboots the board
5. Waits, then re-checks health — SBC comes back online

### Adding a new board

> "Add a new Raspberry Pi to the lab for the SmartHub project"

The assistant calls:
1. `add_sbc(name="rpi-5", project="SmartHub", description="Raspberry Pi 5")`
2. `set_network_address(sbc_name="rpi-5", address_type="ethernet", ip_address="192.168.4.110")`
3. `assign_power_plug(sbc_name="rpi-5", plug_type="kasa", address="192.168.4.140", index=3)`

## Security Considerations

The MCP server can do anything the lab can: power boards on and off, flash
and rewrite SD cards, type into serial consoles, drive actuators and edit
the inventory. An AI agent connected to it should be treated like a
colleague with a shell on the lab host, and the controls below exist to
keep its mistakes (and anyone who reaches the endpoint) contained. Design
decisions: D011, D012, D014 in [`DECISIONS.md`](DECISIONS.md).

| Layer | What it does | Default | Configure |
|---|---|---|---|
| Transport | stdio: only the local user running the client. HTTP: loopback unless authenticated; [TLS](#tls) optional | stdio / `127.0.0.1`, plain HTTP | `--http`, `--host`, `--cert`/`--key` |
| [Authentication](#authentication) | HTTP requests need a web user's API key as a bearer token; sessions, claims and confirm tokens are per user | off (`auth.enabled: false`) | `auth.enabled`, `auth.users[].api_key` |
| [Host header checks](#host-header-checks) | Rejects unexpected `Host`/`Origin` (DNS rebinding) | loopback names | `mcp.allowed_hosts`, `mcp.allowed_origins` |
| [Destructive confirmation](#confirmation-for-destructive-tools) | 24 destructive tools act only on a second call with a single-use token | on | `mcp.confirm_destructive`, `mcp.confirm_exempt` |
| [Host file allowlist](#host-file-access-allowlist) | Tools may only read/write host files under listed directories, opened without following symlinks | deny all | `mcp.allowed_read_paths`, `mcp.allowed_write_paths` |
| [Image uploads](#uploading-images) | `PUT /images/<name>` into one directory: bare names, size and free-space limits, atomic replace, only the uploader or an operator replaces or deletes | off | `mcp.upload_dir`, `mcp.max_upload_mb` |
| Privileged actuator tools | `actuator_add/remove/set` (raw actuator access, bypassing bindings) | off | `mcp.allow_admin_actuator_ops` |
| Claims | An agent's claim on a board blocks other agents' calls that operate or reconfigure it; only operators may force-release; optionally, changing a board requires claiming it | on (`require_claim` off) | `claims.*`, `auth.users[].role` |
| Serialization | One tool call (and hardware-reading resource) at a time | always | — |
| Audit | Every change is recorded with the actor (`<session>[:<user>]`) | always | `labctl activity tail` / `export`, web `/activity` page |
| systemd unit | `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`: on disk, only `/var/lib/labctl` is writable, plus private `/tmp` and `/var/tmp` and the host's shared `/dev` (including `/dev/shm`). Devices are outside this: SD cards are written through the SDWire `sudo` rules (`dd`, `mount`, `partprobe`; D008b), serial ports through group permissions | as shipped | `config/systemd/labctl-mcp.service` |

Every tool and resource is classified (read / db-write / shared-resource /
system-write / hardware / destructive) in
[`docs/OPERATIONS.md`](OPERATIONS.md), alongside the CLI commands.

**Known limits** (by design, for now):

- No read-only tier: any client that can connect can call every tool
  (#7; [`OPERATIONS.md`](OPERATIONS.md) documents the classes only).
- Without auth, all HTTP clients share one identity, so claims and confirm
  tokens don't tell them apart; anyone with local access to the port can
  call every tool.
- Without `--cert`/`--key` the server speaks plain HTTP. Beyond the local
  host, turn TLS on, or use an SSH tunnel or a TLS-terminating reverse
  proxy, and enable auth **before** adding a proxy: a proxy that forwards
  `Host: 127.0.0.1` can't be detected.
- The CLI is not restricted by any of the `mcp.*` settings; it runs with
  the invoking user's permissions and asks for confirmation on a terminal
  (`--yes` to skip).
- API keys are stored in plain text in the config files
  (`/etc/labctl/config.yaml` and the service copy
  `/var/lib/labctl/.config/labctl/config.yaml`); `install-services.sh` and
  `update.sh` make both mode `640`, readable only by root/`labctl` and the
  `labctl` group. Anyone in that group can read every key.

### Deployment checklist

1. Leave `--host` at `127.0.0.1` unless clients must connect from other
   machines; prefer an SSH tunnel to opening the port.
2. For any shared or remote use, set `auth.enabled: true`, give each person
   or agent their own user and `api_key` (`labctl user generate-key`), and
   add the `Authorization: Bearer <api_key>` header to their MCP client
   ([Connecting Claude Code agents](CLAUDE_CODE_AGENTS.md)). Clients on
   other machines should connect over [TLS](#tls) (or a tunnel).
3. Behind a reverse proxy: terminate TLS there, keep the server on
   loopback, and list the public name in `mcp.allowed_hosts`.
4. List the directories MCP tools may use in `mcp.allowed_read_paths`
   (images, files to copy) and `mcp.allowed_write_paths` (boot-test
   output); nothing is allowed until you do. The install script creates
   `/var/lib/labctl/images` and `/var/lib/labctl/output` for this, and
   fresh installs' example config lists them; existing installs must add
   them (`update.sh` prints the lines).
5. Keep `mcp.confirm_destructive` on; exempt only tools you have to
   (e.g. `serial_send` for heavy console work).
6. When several agents share boards, keep claims on (`claims.enabled`, the
   default) and ask agents to claim before working. Over HTTP, claims only
   tell agents apart when auth is enabled (each user is a claimant).
7. Review the audit trail (`labctl activity tail` / `labctl activity
   export`, or the web `/activity` page) after
   unattended runs.

`scripts/update.sh` reports missing allowlist settings (item 4) before
restarting. After the restart it shows `labctl-mcp`'s journal lines from
that restart: the auth mode (item 2: `MCP HTTP: API keys required` /
`no authentication`, with a reminder about the `Authorization` header
whenever keys are required) and startup warnings, which the server logs
whatever `log_level` says. For any service that failed to start, it shows
that service's log since the restart.
To check a config change without restarting, run the check as the
service's user, which may read files you can't:
`sudo -u labctl /opt/labctl/venv/bin/labctl -c /etc/labctl/config.yaml mcp --http 8080 --check`.

### Confirmation for destructive tools

Tool annotations (`destructiveHint` etc.) are only hints a client may
ignore, so the server enforces confirmation itself. By default every tool
annotated destructive (power off/cycle, flashing, SD file changes,
`sdwire_to_host`, `serial_send`, `boot_test`, deleting records and
images, raw actuator and binding verbs, recovery, force release: 24
tools) is a two-step call:

1. Called normally, it **does nothing** and returns a plan:
   ```json
   {"status": "confirmation_required", "tool": "power_off",
    "action": "Power off SBC", "arguments": {"sbc_name": "pi-5-1"},
    "confirm_token": "…", "expires_in_seconds": 120, "message": "…"}
   ```
   `action` is the tool's title. `serial_send` adds a `will_do` object
   with the exact bytes it will write (`bytes_hex`, `byte_count`).
   `power_cycle`, `enter_recovery`, `exit_recovery`, `boot_test` and
   `sdwire_update` with `reboot=true` add `will_do.cycle_delay_seconds`,
   the delay they will wait between off and on, and `cycle_delay_source`,
   where it came from (the largest minimum among the boards on the
   outlet, as for `power_cycle`, is the default and the minimum). An out-of-range
   `delay` or stored delay refuses the call here, with no token. The token
   is bound to that plan: if it differs when you confirm (say the stored
   delay was changed), the call is refused (`confirmation_failed`) and
   you ask for a new plan.
2. Called again with **the same arguments** plus `confirm_token`, it acts.

Tokens are random, single-use, expire after 120 s, and are bound to the tool
and its exact arguments (defaults included); over authenticated HTTP they
are also bound to the user they were issued to. A token used for a different
call or by a different user, used twice, or expired returns
`confirmation_failed` and does nothing.
These tools show an extra optional `confirm_token` parameter in their
schema, and their description ends with a note starting "DESTRUCTIVE. By
default this requires confirmation".

```yaml
mcp:
  confirm_destructive: true      # default; false turns the step off
  confirm_exempt: [serial_send]  # tools that skip it, e.g. for console work
```

With the step turned off or a tool exempted, the **first call acts
immediately**. Clients should never call a destructive tool "to preview":
if the response is not `status: confirmation_required`, the action was
performed. Only an explicit `false` disables the step (a blank value keeps
it on), and unknown names in `confirm_exempt` are logged at startup.

### Host file access (allowlist)

Some tools take paths on the **host** machine: `flash_image` (`image_path`,
`post_flash_copies` sources), `sdwire_update` (`copies` sources) and
`boot_test` (`image`, `output_dir`). Over MCP these are **denied unless
allowlisted** in `config.yaml`:

```yaml
mcp:
  allowed_read_paths:          # images, files to copy onto SD cards
    - /var/lib/labctl/images
  allowed_write_paths:         # boot_test output_dir
    - /var/lib/labctl/output
```

- Host paths must be absolute. They are resolved (symlinks and `..`) before
  the check, so a link or `../` inside an allowed directory cannot reach
  outside it. The checked file is then opened immediately, without
  following any symlink on its path, and everything after that (flashing,
  copying, writing boot-test output) uses that open handle, never the path.
  Swapping a file or directory for a symlink after the check has no effect,
  and raw images are streamed into `sudo dd` rather than opened by root.
  The image format and the copied file's name follow the path you gave: a
  symlink `latest.img.xz` -> `build-4711` is still flashed as xz, and
  `Image` -> `Image-6.1.55` copied into a directory lands as `Image`.
- A bare name with no `/` (`sdcard.img`, or `zImage:zImage` as a copy)
  means that file in `mcp.upload_dir`; see
  [Uploading images](#uploading-images). The allowlist still applies to
  it, so `upload_dir` must be inside `allowed_read_paths`. Dot files there
  (uploads still in progress) can't be read, and tools never write into
  `upload_dir` (a `boot_test` `output_dir` there is refused), even if it
  is also in `allowed_write_paths`.
- Paths on the SD card itself (`dest`, `renames`, `deletes`, and the
  `sdwire_ls`/`sdwire_cat` `path`) are not host paths and are not affected.
- The CLI is not restricted: it already runs with the invoking user's
  permissions.
- `scripts/install-services.sh` creates both default directories
  (`labctl:labctl`; `images/` mode `2775` so members of the `labctl` group
  can drop images in; `output/` mode `3775`, sticky, so they can't replace
  the service's run files). Boot-test run files are written with
  `O_NOFOLLOW`, so a symlink planted in `output/` is refused, not followed.
  Fresh installs get the config above from
  `config/labctl.yaml.example`. On existing installs `scripts/update.sh`
  creates the directories and prints the config to add; it never edits
  config files.
- The systemd unit already confines the service: `ProtectSystem=strict`
  with `ReadWritePaths=/var/lib/labctl`, and `ProtectHome=yes`.

### Uploading images

A tool's arguments are text the model writes out, so an image can't be
passed to a tool. Clients upload it over HTTP to the MCP server itself,
with their own shell and the same API key, and then pass its bare name to
`flash_image`, `sdwire_update` or `boot_test`. That lets an agent build on
one machine and flash on the lab host with only the MCP connection
between them.

```yaml
mcp:
  upload_dir: /var/lib/labctl/images    # empty (default): uploads off
  allowed_read_paths: [/var/lib/labctl/images]
  # max_upload_mb: 8192                 # per upload
```

```bash
curl --fail-with-body -T build/sdcard.img \
  -H "Authorization: Bearer $LABCTL_API_KEY" \
  "https://lab.example.com:8080/images/sdcard.img?sha256=$(sha256sum build/sdcard.img | cut -d' ' -f1)"
```

- `PUT /images/<name>` on the HTTP transport's port, next to `/mcp`. The
  route checks the API key (when auth is on) and the `Host`/`Origin`
  headers as `/mcp` does. stdio servers have no upload route. A stdio
  session runs on the lab host, though, so it can pass an image's
  absolute path inside `mcp.allowed_read_paths` directly (add a build
  directory there to use it in place), copy the image with its shell into
  `upload_dir` (to a unique temp name, then `mv` it into place) and pass the bare
  name, or upload to the host's HTTP service if it uses the same
  `upload_dir`. The last two need `upload_dir` inside
  `allowed_read_paths`, and all three assume the client starts `labctl
  mcp` on the lab host (not over ssh). Over stdio, `list_images` spells out these
  alternatives with this server's paths.
- Names are 1-128 letters, digits, `.`, `_`, `+` or `-`, starting with a
  letter or digit: no paths.
- `Content-Length` is required (`curl -T` sends it). Bodies over
  `mcp.max_upload_mb`, or that would leave less than 1 GiB free on the
  file system (counting what uploads in progress still have to write),
  are refused before anything is written.
- The body goes to a temp file (mode `600`), renamed into place (mode
  `644`, writable by the service only) only when complete and, with
  `?sha256=HEX`, only if the checksum matches. A failed upload stores
  nothing, and a tool already reading the old image keeps reading it.
- An existing image is replaced only with `?overwrite=1`, and only by the
  user who uploaded it or an operator. Files put in the directory another
  way (scp, the CLI), or changed since they were uploaded, have no
  uploader, so only operators may replace them. `delete_image` follows
  the same rule.
- Uploads (accepted or refused) and deletions are in the activity log
  (`image_upload`, `image_delete`). The `images` table (schema v10)
  records each upload's user, size and SHA-256; `list_images` shows them.
- Responses are JSON: `201` (new) or `200` (replaced) with `name`, `path`,
  `size`, `sha256`, `replaced`; errors carry `error` and `message`:

  | Status | `error` |
  |---|---|
  | `400` | `invalid_name`, `invalid_sha256`, `invalid_length`, `length_mismatch` (body longer or shorter than `Content-Length`), `disconnected` |
  | `401` | `unauthorized` (no valid API key, when auth is on) |
  | `403` | `not_owner`, `uploads_disabled` |
  | `409` | `exists` (add `?overwrite=1`), `not_a_file` |
  | `411` | `length_required` |
  | `413` | `too_large` |
  | `422` | `sha256_mismatch` |
  | `500` | `upload_dir_unavailable`, `upload_failed` |
  | `507` | `insufficient_space` |

  A rejected `Host` header gets a plain-text `421`, a rejected `Origin` a
  plain-text `403`, as on `/mcp`. `delete_image` returns these errors
  as `Error: <message>` text (an unknown name: `no image named …`).

## Troubleshooting

### Server won't start

Check that the `mcp` package is installed:
```bash
python -c "import importlib.metadata as m; print(m.version('mcp'))"
```

### Tools return "SBC not found"

The MCP server reads from the same database as the CLI. Ensure the config
file is accessible and `database_path` points to the correct database.

### Power operations fail

Kasa devices require credentials in the config file:
```yaml
kasa:
  username: "your-tplink-email"
  password: "your-tplink-password"
```

### Logging

In stdio mode, all logs go to stderr. To see debug output:
```bash
LABCTL_LOG_LEVEL=DEBUG labctl mcp 2>mcp-debug.log
```
