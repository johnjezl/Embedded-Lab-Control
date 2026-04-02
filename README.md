# Embedded Lab Control (labctl)

Centralized management system for embedded development lab resources. Provides deterministic USB serial access, power control, and resource tracking for multiple single-board computers (SBCs).

## Features

- **Deterministic USB Serial Naming** - udev rules create stable `/dev/lab/<sbc>` symlinks
- **Remote Serial Console** - TCP access via ser2net with multi-client support
- **Smart Plug Power Control** - Supports Tasmota, Kasa, and Shelly devices
- **Web Dashboard** - Browser-based monitoring and control
- **REST API** - Programmatic access to all features
- **Health Monitoring** - Automated ping, serial, and power checks
- **Session Logging** - Capture serial output with rotation and compression
- **Authentication** - Session-based web login and API key auth (optional, disabled by default)

## Installation

### Prerequisites

```bash
# System packages
sudo apt install python3-venv ser2net

# Clone repository
git clone https://github.com/johnjezl/Embedded-Lab-Control.git
cd Embedded-Lab-Control
```

### Setup

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install package
pip install -e .

# Copy example config
mkdir -p ~/.config/labctl
cp config/labctl.yaml.example ~/.config/labctl/config.yaml

# Install udev rules (optional, for deterministic naming)
sudo ./scripts/install-udev.sh
```

## Quick Start

```bash
# Add an SBC
labctl add rpi4 --description "Raspberry Pi 4"

# Assign a serial port
labctl port assign rpi4 /dev/lab/rpi4 --baud 115200

# Assign a power plug (Tasmota example)
labctl plug assign rpi4 tasmota 192.168.1.100

# View status
labctl status

# Connect to serial console
labctl console rpi4

# Control power
labctl power rpi4 on
labctl power rpi4 cycle --delay 5

# Start web dashboard
labctl web --port 5000
```

## CLI Reference

### Global Options

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose (DEBUG) output |
| `-q`, `--quiet` | Suppress non-essential output |
| `-d`, `--delay <seconds>` | Wait before executing the command |
| `-c`, `--config <path>` | Path to config file |

### SBC Management

| Command | Description |
|---------|-------------|
| `labctl add <name>` | Add a new SBC |
| `labctl remove <name>` | Remove an SBC |
| `labctl list` | List all SBCs |
| `labctl info <name>` | Show SBC details |
| `labctl edit <name>` | Edit SBC properties (--rename, --project, etc.) |
| `labctl status` | Show status overview |
| `labctl status --watch` | Continuous status monitoring |

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
| `labctl sdwire flash <sbc> <image>` | Flash full image to SD card and reboot SBC |
| `labctl sdwire update <sbc> -p N -c src:dest -r old:new -d file` | Copy, rename, or delete files on a partition |

### Serial Device Management

| Command | Description |
|---------|-------------|
| `labctl serial discover` | Scan for connected USB-serial adapters |
| `labctl serial add <name> <usb_path>` | Register a USB-serial adapter |
| `labctl serial remove <name>` | Unregister an adapter |
| `labctl serial list` | List all registered adapters |
| `labctl serial rename <name> <new>` | Rename an adapter |
| `labctl serial udev --install --reload` | Generate and apply udev rules |

### Serial Console

| Command | Description |
|---------|-------------|
| `labctl port assign <sbc> <type> [device]` | Assign serial port (-s, --alias) |
| `labctl port list` | List port assignments |
| `labctl port list --unassigned` | Show unassigned /dev/lab/* devices |
| `labctl connect <alias\|sbc\|device>` | Connect by alias, SBC name, or device |
| `labctl console <sbc>` | Connect to serial console |
| `labctl log <sbc>` | Capture serial to log file |
| `labctl log <sbc> --follow` | Stream serial output to terminal |

### Power Control

| Command | Description |
|---------|-------------|
| `labctl plug assign <sbc> <type> <addr>` | Assign power plug (--index for strips) |
| `labctl power <sbc> on` | Turn on |
| `labctl power <sbc> off` | Turn off |
| `labctl power <sbc> cycle` | Power cycle |
| `labctl power <sbc> status` | Check power state |
| `labctl power-all on/off` | Control all SBCs |

### Multi-Client Access

| Command | Description |
|---------|-------------|
| `labctl proxy start <sbc>` | Start serial proxy |
| `labctl proxy list` | List running proxies |
| `labctl sessions <sbc>` | Show connected clients |

### Health Monitoring

| Command | Description |
|---------|-------------|
| `labctl health-check` | Run all health checks |
| `labctl health-check --type ping` | Ping check only |
| `labctl health-check --sbc <name>` | Check single SBC |
| `labctl monitor --foreground` | Start monitoring daemon |

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
| `labctl mcp --http 8080` | Start MCP server (HTTP transport) |

### Aliases

For convenience, common aliases are supported:
- `ls` → `list`
- `rm`, `delete` → `remove`
- `show` → `info`
- `on` → `power on`
- `off` → `power off`

## Configuration

Default config location: `~/.config/labctl/config.yaml`

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
```

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

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sbcs` | GET | List all SBCs |
| `/sbcs/<name>` | GET | Get SBC details |
| `/sbcs` | POST | Create SBC |
| `/sbcs/<name>` | DELETE | Remove SBC |
| `/sbcs/<name>/power` | POST | Control power (`{"action": "on/off/cycle"}`) |
| `/sbcs/<name>/health` | GET | Get health status |
| `/sbcs/<name>/uptime` | GET | Get uptime statistics |
| `/health` | GET | System health (always open, no auth required) |

## MCP Server (AI Integration)

labctl includes an MCP (Model Context Protocol) server for AI assistant integration.
This allows tools like Claude Desktop and Claude Code to manage lab resources directly.

Install the MCP dependency:

```bash
pip install labctl[mcp]
```

**Resources** (read-only data): `lab://sbcs`, `lab://sbcs/{name}`, `lab://power/{name}`,
`lab://serial-devices`, `lab://ports`, `lab://health/{name}`, `lab://status`

**Tools** (actions): `power_on`, `power_off`, `power_cycle`, `run_health_check`,
`add_sbc`, `remove_sbc`, `update_sbc`, `assign_serial_port`, `assign_power_plug`,
`set_network_address`

**Prompts**: `debug-sbc` (guided SBC debugging), `lab-report` (comprehensive status)

### Claude Desktop / Claude Code Configuration

Add to your MCP client configuration:

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

## Systemd Services

Install systemd services for production use:

```bash
sudo ./scripts/install-services.sh
sudo systemctl enable --now labctl-web
sudo systemctl enable --now labctl-monitor

# Optional: MCP server for remote AI integration (HTTP on port 8080)
sudo systemctl enable --now labctl-mcp
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

## License

MIT
