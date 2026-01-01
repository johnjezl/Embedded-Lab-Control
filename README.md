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

### SBC Management

| Command | Description |
|---------|-------------|
| `labctl add <name>` | Add a new SBC |
| `labctl remove <name>` | Remove an SBC |
| `labctl list` | List all SBCs |
| `labctl info <name>` | Show SBC details |
| `labctl edit <name>` | Edit SBC properties |
| `labctl status` | Show status overview |
| `labctl status --watch` | Continuous status monitoring |

### Serial Console

| Command | Description |
|---------|-------------|
| `labctl port assign <sbc> <port>` | Assign serial port |
| `labctl port list` | List port assignments |
| `labctl port list --unassigned` | Show unassigned /dev/lab/* devices |
| `labctl console <sbc>` | Connect to serial console |
| `labctl log <sbc>` | Capture serial to log file |
| `labctl log <sbc> --follow` | Stream serial output to terminal |

### Power Control

| Command | Description |
|---------|-------------|
| `labctl plug assign <sbc> <type> <addr>` | Assign power plug |
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

database_path: ~/.config/labctl/labctl.db
log_level: INFO
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

## REST API

Base URL: `http://localhost:5000/api`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sbcs` | GET | List all SBCs |
| `/sbcs/<name>` | GET | Get SBC details |
| `/sbcs` | POST | Create SBC |
| `/sbcs/<name>` | DELETE | Remove SBC |
| `/sbcs/<name>/power` | POST | Control power (`{"action": "on/off/cycle"}`) |
| `/sbcs/<name>/health` | GET | Get health status |
| `/sbcs/<name>/uptime` | GET | Get uptime statistics |

## Systemd Services

Install systemd services for production use:

```bash
sudo ./scripts/install-services.sh
sudo systemctl enable --now labctl-web
sudo systemctl enable --now labctl-monitor
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
