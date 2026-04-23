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
│  (FastMCP)                   │
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
labctl mcp --http 8080
```

Uses the Streamable HTTP transport on the specified port. Useful for
multi-client scenarios or accessing the lab from a different machine.

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

The service runs on port 8080 by default. Edit the service file to change the port.

Remote clients connect via HTTP:

```json
{
  "mcpServers": {
    "labctl": {
      "url": "http://tarrasque:8080/mcp"
    }
  }
}
```

**Note:** The HTTP transport currently has no authentication. Only run on
trusted networks or behind a reverse proxy with auth.

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
| `sdwire_update` | `sbc_name`, `partition`, `copies`, `renames`, `deletes`, `reboot` | Copy, rename, or delete files on SD card partition (atomic: mount, operate, unmount) |
| `sdwire_ls` | `sbc_name`, `partition`, `path`, `recursive`, `max_entries` | List directory contents on an SD card partition using a read-only mount |
| `sdwire_cat` | `sbc_name`, `partition`, `path`, `max_bytes`, `encoding` | Read a file from an SD card partition with size and encoding guards |
| `sdwire_info` | `sbc_name` | Return partition-table and filesystem metadata for the SD card |
| `sdwire_add` | `name`, `serial_number`, `device_type` | Register an SDWire device |
| `sdwire_remove` | `name` | Unregister an SDWire device |
| `sdwire_assign` | `sbc_name`, `device_name` | Assign SDWire device to SBC |
| `sdwire_unassign` | `sbc_name` | Remove SDWire assignment from SBC |
| `sdwire_discover` | | Scan for connected SDWire devices |
| `flash_image` | `sbc_name`, `image_path`, `reboot`, `post_flash_copies` | Flash raw disk image (.img/.img.xz/.img.gz) to SD card with safety checks |

### Serial I/O

| Tool | Parameters | Description |
|------|------------|-------------|
| `serial_capture` | `port_name`, `timeout`, `until_pattern`, `tail` | Capture serial output until timeout or pattern match |
| `serial_send` | `port_name`, `data`, `newline`, `capture_timeout`, `capture_until` | Send data to serial port, optionally capture response |

### Boot Testing

| Tool | Parameters | Description |
|------|------------|-------------|
| `boot_test` | `sbc_name`, `expect_pattern`, `runs`, `timeout`, `image`, `dest`, `partition`, `output_dir` | Automated boot reliability testing with deploy and serial capture |

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

- The MCP server has full access to lab resources — it can power cycle boards,
  modify the database, and run health checks.
- In stdio mode, access is limited to the local user running the client.
- In HTTP mode, there is currently no authentication on the MCP endpoint.
  Use this only on trusted networks or behind a reverse proxy with auth.
- The server uses the same configuration and database as the CLI, so all
  operations are audited in the audit_log table.

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
