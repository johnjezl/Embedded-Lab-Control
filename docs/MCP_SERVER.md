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
pip install labctl[mcp]

# Or if installing from source:
pip install ".[mcp]"

# In the production venv:
sudo /opt/labctl/venv/bin/pip install /path/to/Embedded-Lab-Control[mcp]
```

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
```

Uses the Streamable HTTP transport (endpoint `/mcp`) on the specified port.
Useful for multi-client scenarios or accessing the lab from a different machine.

#### Authentication

HTTP authentication uses the same users and API keys as the web REST API
(D012): when `auth.enabled` is `true`, every request must carry
`Authorization: Bearer <api_key>` for a user in `auth.users`, otherwise it
gets `401`. Generate a key with `labctl user generate-key` and put it in
that user's `api_key`.

- Keys are re-read from the config on every request, so removing or
  rotating a key takes effect without restarting the server.
- Each MCP session belongs to the user who opened it; another user's key
  can't drive it, and destructive-tool `confirm_token`s are only valid for
  (and only consumed by) the user they were issued to.
- Each user is a separate claimant: one user's claim blocks another's
  mutating calls, and the default agent name is the username. Claims and
  audit entries record `<server session>:<username>`.
- The server **refuses to start** without auth on a non-loopback `--host`,
  or when `mcp.allowed_hosts` / `allowed_origins` accept a non-loopback
  name (a reverse proxy). It can't detect a proxy that forwards
  `Host: 127.0.0.1` (nginx's default `proxy_pass` behaviour), so **always
  enable auth before putting a proxy in front**. It also refuses
  `auth.enabled: true` when no user has both an `api_key` and a username
  (quote numeric usernames in YAML).
- Without auth (the default), it listens on loopback only, all clients
  share one claimant identity, and anyone who can connect locally can call
  every tool. Every HTTP start prints its mode (`MCP HTTP: API keys
  required` / `MCP HTTP: no authentication (loopback only)`).
- `labctl mcp --http PORT [--host ADDR] --check` runs these startup checks
  (the same code the server runs before serving) and exits: it prints
  `auth: required` / `auth: none` (or `n/a` without `--http`) and the
  config file it loaded, plus any startup warnings, whatever `log_level`
  says. It exits with status 1 if the server would refuse to
  start, or if a config file it would read can't be read or parsed (the
  server would start anyway, on the next file in the search order or on
  built-in defaults, logging a warning).

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
bind a reachable address with auth enabled. The server itself speaks plain
HTTP, so keys cross the network in clear text unless a tunnel or proxy
provides TLS.

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

Add to your Claude Code MCP settings (`.claude/settings.json` or project settings):

```json
{
  "mcpServers": {
    "labctl": {
      "command": "/opt/labctl/venv/bin/labctl",
      "args": ["mcp"]
    }
  }
}
```

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
      "args": ["mcp"]
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

### Template Resources (parameterized)

| URI | Parameters | Description |
|-----|------------|-------------|
| `lab://sbcs/{sbc_name}` | SBC name | Full details for one SBC |
| `lab://power/{sbc_name}` | SBC name | Live power state query (on/off/unknown) |
| `lab://health/{sbc_name}` | SBC name | Live health check (ping, serial, power) |
| `lab://claims` | — | All active claims across the lab |
| `lab://claims/{sbc_name}` | SBC name | Current claim on an SBC |
| `lab://claims/history/{sbc_name}` | SBC name | Past (released) claims for an SBC |
| `lab://claims/metrics` | — | Aggregate claim statistics (totals by outcome, avg duration) |
| `lab://activity/recent` | — | Last 50 activity events across the lab |
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
| `power_on` | `sbc_name` | Turn on power to an SBC |
| `power_off` | `sbc_name` | Turn off power to an SBC |
| `power_cycle` | `sbc_name`, `delay` (default 2.0) | Power cycle (off, wait, on) |

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
| `remove_serial_device` | `name` | Unregister a USB-serial adapter |
| `serial_discover` | | Scan for connected USB-serial adapters |

### SDWire (SD Card Multiplexer)

| Tool | Parameters | Description |
|------|------------|-------------|
| `sdwire_to_dut` | `sbc_name` | Switch SD card to SBC (boot from SD) |
| `sdwire_to_host` | `sbc_name` | Switch SD card to host (for flashing) |
| `sdwire_update` | `sbc_name`, `partition`, `copies`, `renames`, `deletes`, `reboot` | Copy, rename, or delete files on SD card partition (atomic: mount, operate, unmount). Host `copies` sources must be allowlisted |
| `sdwire_ls` | `sbc_name`, `partition`, `path`, `recursive`, `max_entries` | List directory contents on an SD card partition using a read-only mount |
| `sdwire_cat` | `sbc_name`, `partition`, `path`, `max_bytes`, `encoding` | Read a file from an SD card partition with size and encoding guards |
| `sdwire_info` | `sbc_name` | Return partition-table and filesystem metadata for the SD card |
| `sdwire_add` | `name`, `serial_number`, `device_type` | Register an SDWire device |
| `sdwire_remove` | `name` | Unregister an SDWire device |
| `sdwire_assign` | `sbc_name`, `device_name` | Assign SDWire device to SBC |
| `sdwire_unassign` | `sbc_name` | Remove SDWire assignment from SBC |
| `sdwire_discover` | | Scan for connected SDWire devices |
| `flash_image` | `sbc_name`, `image_path`, `reboot`, `post_flash_copies` | Flash raw disk image (.img/.img.xz/.img.gz) to SD card with safety checks. Host paths must be allowlisted (see [Host file access](#host-file-access-allowlist)) |

### Serial I/O

| Tool | Parameters | Description |
|------|------------|-------------|
| `serial_capture` | `port_name`, `timeout`, `until_pattern`, `tail` | Capture serial output until timeout or pattern match |
| `serial_send` | `port_name`, `data`, `newline`, `capture_timeout`, `capture_until` | Send data to serial port, optionally capture response |

### Boot Testing

| Tool | Parameters | Description |
|------|------------|-------------|
| `boot_test` | `sbc_name`, `expect_pattern`, `runs`, `timeout`, `image`, `dest`, `partition`, `output_dir` | Automated boot reliability testing with deploy and serial capture. `image` must be in `allowed_read_paths`, `output_dir` in `allowed_write_paths` |

### Claims (Exclusive Access Coordination)

| Tool | Parameters | Description |
|------|------------|-------------|
| `claim_sbc` | `sbc_name`, `duration_minutes`, `reason`, `agent_name`, `context` | Claim exclusive access to an SBC |
| `release_sbc` | `sbc_name` | Release a claim held by the calling session |
| `renew_sbc_claim` | `sbc_name`, `duration_minutes` | Extend an active claim's deadline |
| `list_claims` | — | List all active claims across the lab |
| `get_claim` | `sbc_name` | Get the current claim on an SBC |
| `request_sbc_release` | `sbc_name`, `reason` | Politely ask the claimant to release |
| `force_release_sbc` | `sbc_name`, `reason` | Operator override — forcibly release |

**Claim enforcement:** Mutating tools (`power_on/off/cycle`, `serial_send`,
`sdwire_to_host/dut`, `sdwire_update`, `flash_image`, `boot_test`, `remove_sbc`)
check for active claims. If another agent holds the claim, a structured JSON
error is returned with `"error": "sbc_claimed"` and hints. Claimant operations
proceed and implicitly heartbeat the claim.

### Tool Return Values

All tools return strings. Success messages are plain text, structured data is
returned as formatted JSON. Errors are prefixed with "Error:" or returned as
structured JSON with an `"error"` field (claim conflicts).

## Prompts (Guided Workflows)

Prompts are reusable instruction templates that guide the AI through multi-step
workflows.

### `debug-sbc`

**Parameters:** `sbc_name`

Guides the assistant through debugging an unresponsive SBC:
1. Check SBC state via `lab://sbcs/{name}` resource
2. Check power via `lab://power/{name}` resource
3. Run health check via `run_health_check` tool
4. Take corrective action based on findings (power cycle, etc.)

### `lab-report`

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

Using the `debug-sbc` prompt, the assistant:
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
decisions: D011, D012 in [`DECISIONS.md`](DECISIONS.md).

| Layer | What it does | Default | Configure |
|---|---|---|---|
| Transport | stdio: only the local user running the client. HTTP: loopback unless authenticated | stdio / `127.0.0.1` | `--http`, `--host` |
| [Authentication](#authentication) | HTTP requests need a web user's API key as a bearer token; sessions, claims and confirm tokens are per user | off (`auth.enabled: false`) | `auth.enabled`, `auth.users[].api_key` |
| [Host header checks](#host-header-checks) | Rejects unexpected `Host`/`Origin` (DNS rebinding) | loopback names | `mcp.allowed_hosts`, `mcp.allowed_origins` |
| [Destructive confirmation](#confirmation-for-destructive-tools) | 23 destructive tools act only on a second call with a single-use token | on | `mcp.confirm_destructive`, `mcp.confirm_exempt` |
| [Host file allowlist](#host-file-access-allowlist) | Tools may only read/write host files under listed directories, opened without following symlinks | deny all | `mcp.allowed_read_paths`, `mcp.allowed_write_paths` |
| Privileged actuator tools | `actuator_add/remove/set` (raw actuator access, bypassing bindings) | off | `mcp.allow_admin_actuator_ops` |
| Claims | An agent's claim on a board blocks other agents' mutating calls on it | on | `claims.*` |
| Serialization | One tool call (and hardware-reading resource) at a time | always | — |
| Audit | Every change is recorded with the actor (`<session>[:<user>]`) | always | `labctl activity tail` / `export`, web `/activity` page |
| systemd unit | `ProtectSystem=strict`, `ProtectHome=yes`, writes only to `/var/lib/labctl` | as shipped | `config/systemd/labctl-mcp.service` |

Every tool and resource is classified (read / db-write / shared-resource /
system-write / hardware / destructive) in
[`docs/OPERATIONS.md`](OPERATIONS.md), alongside the CLI commands.

**Known limits** (by design, for now):

- No read-only tier: any client that can connect can call every tool
  (#7; [`OPERATIONS.md`](OPERATIONS.md) documents the classes only).
- Without auth, all HTTP clients share one identity, so claims and confirm
  tokens don't tell them apart; anyone with local access to the port can
  call every tool.
- The server speaks plain HTTP. Beyond the local host, use an SSH tunnel or
  a TLS-terminating reverse proxy, and enable auth **before** adding a
  proxy: a proxy that forwards `Host: 127.0.0.1` can't be detected.
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
   add the `Authorization: Bearer <api_key>` header to their MCP client.
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
To check a config change without restarting, run
`labctl -c FILE mcp --http PORT --check`.

### Confirmation for destructive tools

Tool annotations (`destructiveHint` etc.) are only hints a client may
ignore, so the server enforces confirmation itself. By default every tool
annotated destructive (power off/cycle, flashing, SD file changes,
`serial_send`, deleting records, actuator/binding verbs, recovery, force
release: 23 tools) is a two-step call:

1. Called normally, it **does nothing** and returns a plan:
   ```json
   {"status": "confirmation_required", "tool": "power_off",
    "arguments": {"sbc_name": "pi-5-1"}, "confirm_token": "…",
    "expires_in_seconds": 120, "message": "…"}
   ```
2. Called again with **the same arguments** plus `confirm_token`, it acts.

Tokens are random, single-use, expire after 120 s, and are bound to the tool
and its exact arguments (defaults included); over authenticated HTTP they
are also bound to the user they were issued to. A token used for a different
call or by a different user, used twice, or expired returns
`confirmation_failed` and does nothing.
These tools show an extra optional `confirm_token` parameter in their
schema and say "DESTRUCTIVE: requires confirmation" in their description.

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

## Troubleshooting

### Server won't start

Check that the `mcp` package is installed:
```bash
python -c "import mcp; print(mcp.__version__)"
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
