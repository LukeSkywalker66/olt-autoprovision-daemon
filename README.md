# OLT Auto-Provision Daemon

**Zero-Touch GPON Provisioning for Huawei MA5800 OLTs via Event-Driven Syslog Listening.**

---

## Overview

The OLT Auto-Provision Daemon is a Linux service that passively listens for syslog events (UDP) from Huawei MA5800 OLTs. When a new ONT is auto-discovered by the OLT's `ont auto-add-policy`, the daemon detects the resulting "service port creation incorrect" warning, extracts the ONT identity, and provisions it via SSH — **fully automatic, zero human intervention**.

### How It Works

```
ONT connects → OLT auto-adds ONT → OLT fails auto service-port creation →
Syslog WARNING → Daemon captures event → SSH Worker provisions:
  1. WAN DHCP on management VLAN
  2. TR-069 server profile
  3. Service-port for management VLAN
```

### Key Features

- **Multi-OLT**: Supports N OLTs from a single YAML configuration file indexed by IP.
- **Event-Driven**: Listens on UDP (syslog), never polls. Reacts in real-time.
- **High Concurrency**: asyncio UDP listener + ThreadPoolExecutor for SSH workers. 50 simultaneous provisions without blocking.
- **Deduplication**: TTL-based in-memory cache prevents reprovisioning the same ONT from duplicate syslog events.
- **Legacy-Proven**: SSH/OMCI logic refactored from battle-tested production code.
- **YAML-First**: All provisioning parameters (VLAN, traffic tables, TR-069 profile) per OLT, with global defaults.
- **Extensible**: Strategy pattern for parsers — add ZTE, Nokia, or any OLT model without modifying core code.
- **Systemd Native**: Ships with a hardened systemd unit file for deployment.

---

## Project Structure

```
olt-autoprovision-daemon/
├── config/
│   └── olts.yaml              # Multi-OLT credentials & per-OLT profiles
├── core/
│   ├── __init__.py
│   ├── listener.py             # asyncio UDP syslog server + dedup cache
│   ├── parser.py               # Regex parser (Strategy pattern, parametrizable)
│   └── ssh_worker.py           # SSH provisioning worker (refactored legacy)
├── logs/                       # Rotating log files (auto-created)
├── deploy/
│   └── olt-provision.service   # systemd unit template
├── main.py                     # Application entry point
├── requirements.txt            # Python dependencies (netmiko + pyyaml)
└── README.md                   # This file
```

---

## Requirements

- **Python 3.11+** (uses `asyncio`, `dataclasses`, `str | None` syntax)
- **Linux** (systemd for deployment; can run manually on any OS)
- Network access to OLTs via SSH (port 22 or custom)
- UDP port accessible for syslog reception (default: 5514, or 514 with root)

### Dependencies

```bash
pip install -r requirements.txt
```

| Package | Version | Purpose |
|---------|---------|---------|
| `netmiko` | >=4.3.0 | SSH client for Huawei VRP |
| `pyyaml` | >=6.0 | YAML configuration parsing |

Everything else is Python standard library: `asyncio`, `logging`, `concurrent.futures`, `re`, `argparse`, `dataclasses`, `pathlib`.

---

## Quick Start

### 1. Clone & Install

```bash
git clone <repo-url> /opt/olt-autoprovision-daemon
cd /opt/olt-autoprovision-daemon
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Your OLTs

Edit [`config/olts.yaml`](config/olts.yaml) and add your OLTs indexed by IP:

```yaml
defaults:
  syslog_port: 5514
  management_vlan: 150
  tr069_profile_id: 1
  # ... see file for all defaults

olts:
  "10.11.104.2":                   # ← This is the IP that sends syslog
    name: "NODO-HORNILLOS"
    ssh_user: "smartoltusr"
    ssh_pass: "your_password"
    # All provisioning params inherit from defaults

  "10.11.104.5":
    name: "Villa Dolores 2"
    ssh_user: "smartoltusr"
    ssh_pass: "another_password"
    tr069_profile_id: 2            # Override: this OLT uses a different profile
    traffic_table_up: "SMARTOLT-VOIPMNG-10M"
    traffic_table_down: "SMARTOLT-VOIPMNG-10M"
```

### 3. Configure Your OLTs to Send Syslog

On each Huawei MA5800, configure syslog export to the daemon's IP:

```
syslog
 info-center source default channel 2 log level warning
 info-center loghost 192.168.1.100 facility local1
```

Replace `192.168.1.100` with the IP where the daemon runs.

### 4. Ensure `ont auto-add-policy` is Active

The OLT must have auto-add enabled so that new ONTs are automatically registered:

```
ont auto-add-policy permit
```

### 5. Run

```bash
# Development / testing (non-privileged port)
python main.py

# Production with privileged port 514
sudo python main.py --port 514

# Custom config path
python main.py --config /etc/olt/production-olts.yaml

# Debug mode (verbose logging)
python main.py --debug
```

### 6. Deploy as a Systemd Service

```bash
sudo cp deploy/olt-provision.service /etc/systemd/system/
sudo useradd -r -s /bin/false olt-daemon
sudo chown -R olt-daemon:olt-daemon /opt/olt-autoprovision-daemon
sudo systemctl daemon-reload
sudo systemctl enable --now olt-provision.service
sudo systemctl status olt-provision.service
```

---

## CLI Reference

```
usage: python main.py [-h] [--config CONFIG] [--host HOST] [--port PORT]
                      [--max-workers MAX_WORKERS] [--log-dir LOG_DIR] [--debug]

Options:
  -c, --config CONFIG         Path to olts.yaml (default: config/olts.yaml)
  -H, --host HOST             UDP bind address (default: 0.0.0.0)
  -p, --port PORT             UDP port (default: 5514)
  -w, --max-workers MAX_WORKERS
                              Max concurrent SSH threads (default: 50)
  -l, --log-dir LOG_DIR       Log directory (default: logs/)
  -d, --debug                 Enable DEBUG-level logging
```

---

## Syslog Event Format

The daemon parses the following Huawei MA5800 syslog event:

```
<132> 2000-02-09 00:06:30-03:00 0.0.0.0 ! RUNNING WARNING 2000-02-09 00:06:29-03:00
  EVENT NAME :Parameters for automatic service port creation are incorrect
  PARAMETERS :FrameID: 0, SlotID: 1, PortID: 0, ONT ID: 0, Cause:  Parameters for automatic service port creation of an ONT are not configured or incorrectly configured
```

The parser extracts:
- **fsp**: `"0/1/0"` (assembled from FrameID/SlotID/PortID)
- **ont_id**: `"0"` (ONT ID assigned by OLT)

The OLT source IP is extracted from the UDP packet address (not from the syslog text, which shows `0.0.0.0`).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      main.py                                │
│  ┌──────────────┐  ┌────────────┐  ┌────────────────────┐  │
│  │ load_olt_    │  │ setup_     │  │ SyslogListener     │  │
│  │ config()     │  │ logging()  │  │ .start()           │  │
│  └──────────────┘  └────────────┘  └─────────┬──────────┘  │
│                                              │              │
└──────────────────────────────────────────────┼──────────────┘
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    │           core/listener.py                         │
                    │                                                     │
                    │  ┌─────────────┐   ┌──────────────┐               │
                    │  │ asyncio UDP │   │ OLTRegistry  │               │
                    │  │ :5514       │   │ IP → OLTConfig│               │
                    │  └──────┬──────┘   └──────┬───────┘               │
                    │         │                 │                        │
                    │         ▼                 ▼                        │
                    │  ┌─────────────────────────────────────┐          │
                    │  │        _on_datagram()               │          │
                    │  │  1. Decode syslog                   │          │
                    │  │  2. Lookup OLT by src_ip            │          │
                    │  │  3. parse_syslog_message()          │          │
                    │  │  4. DedupCache.is_duplicate()       │          │
                    │  │  5. executor.submit(ssh_worker)     │          │
                    │  └────────────────┬────────────────────┘          │
                    │                   │                               │
                    │     ┌─────────────▼──────────────┐               │
                    │     │  ThreadPoolExecutor         │               │
                    │     │  (max 50 threads)           │               │
                    │     └─────────────┬──────────────┘               │
                    └───────────────────┼──────────────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────────────┐
                    │           core/ssh_worker.py                     │
                    │                                                   │
                    │  ssh_provision_worker(parsed_event, olt_config)   │
                    │    │                                              │
                    │    ├── HuaweiSSHClient.connect()                  │
                    │    ├── enter_config_mode()                        │
                    │    ├── interface gpon 0/1                         │
                    │    ├── ont ipconfig ... dhcp vlan 150             │
                    │    ├── ont tr069-server-config ... profile-id 1   │
                    │    ├── service-port vlan 150 gpon 0/1/0 ...       │
                    │    ├── quit                                       │
                    │    └── disconnect()                               │
                    └───────────────────────────────────────────────────┘
```

### Design Principles (SOLID)

| Principle | Application |
|-----------|-------------|
| **S**ingle Responsibility | `listener.py` receives UDP, `parser.py` parses, `ssh_worker.py` provisions |
| **O**pen/Closed | `PARSER_REGISTRY` + `BaseSyslogParser` — add new OLT models without touching existing code |
| **L**iskov Substitution | Any `BaseSyslogParser` subclass is interchangeable |
| **I**nterface Segregation | Small, focused interfaces: `parse()`, `provision()`, `connect()` |
| **D**ependency Inversion | Listener depends on `parse_syslog_message()` function and `OLTConfig` dataclass — not concrete implementations |

---

## Logging

All events are logged to both **stdout** (console) and **rotating files** in `logs/`:

| Level | Examples |
|-------|---------|
| `DEBUG` | Non-matching syslog discarded, datagram bytes received |
| `INFO` | ONT auto-discovered, SSH connection established, provisioning OK |
| `WARNING` | Unknown OLT IP, OLT busy (retrying), dedup skip |
| `ERROR` | SSH timeout, authentication failure, command failure |
| `CRITICAL` | Socket bind failure, fatal startup error |

Log files rotate at 10 MB with 5 backups kept.

---

## Adding Support for a New OLT Model

1. Create a new parser class in [`core/parser.py`](core/parser.py):

```python
class ZTEC600Parser(BaseSyslogParser):
    PATTERN = r"..."  # Your regex here

    def parse(self, raw_message: str) -> dict | None:
        ...
```

2. Register it in `PARSER_REGISTRY`:

```python
PARSER_REGISTRY = {
    "huawei_ma5800": HuaweiMA5800Parser,
    "zte_c600": ZTEC600Parser,  # ← add here
}
```

3. Set `parser_model: "zte_c600"` for the OLT in `olts.yaml`.

That's it — no changes needed in `listener.py`, `ssh_worker.py`, or `main.py`.

---

## FAQ

**Q: Why not use the "ONT online" alarm (104001)?**
A: The "ONT online" alarm doesn't reliably contain the ONT ID. The "service port creation incorrect" event fires immediately after the OLT's auto-add-policy assigns the ONT ID and attempts (and fails) to auto-create the service port. It's deterministic and contains the definitive ONT ID.

**Q: What if the OLT sends the same warning multiple times?**
A: The `DedupCache` (TTL-based, default 5 minutes) prevents reprovisioning the same (IP, FSP, ONT_ID) tuple within the TTL window.

**Q: Does the daemon perform `ont add`?**
A: No. The OLT's `ont auto-add-policy` handles ONT registration. The daemon only creates the service layer (WAN DHCP + TR-069 + service-port).

**Q: Can I use this with non-Huawei OLTs?**
A: The architecture supports it via the Strategy pattern in `parser.py`. You need to implement a parser for your OLT model and optionally adapt the `_build_provisioning_commands()` function in `ssh_worker.py` if the VRP commands differ.

---

## License

Internal use — 2F Internet / Oleander Group.

---

## Contributing

This project follows SOLID principles and strict type hints. Before submitting changes:
1. Ensure all type hints are correct (`from __future__ import annotations`).
2. Add docstrings to all public functions/classes.
3. Test with a real or simulated Huawei MA5800 syslog event.
