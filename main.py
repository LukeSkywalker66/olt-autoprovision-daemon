#!/usr/bin/env python3
"""
OLT Auto-Provision Daemon — Zero-Touch GPON Provisioning.

Listens for Huawei MA5800 syslog events (UDP) indicating that a new ONT has been
auto-discovered by the OLT's ont-auto-add-policy. When the OLT emits a warning
about incorrect automatic service-port parameters, the daemon extracts the ONT
identity (Frame/Slot/Port + ONT ID) and provisions it via SSH:
  1. WAN DHCP on management VLAN.
  2. TR-069 server profile injection.
  3. Service-port creation for the management VLAN.

Usage:
    python main.py                              # Defaults (UDP :5514, config/olts.yaml)
    python main.py --port 514                   # Privileged port (requires root/CAP_NET_BIND_SERVICE)
    python main.py --config /etc/olt/olts.yaml  # Custom config path
    python main.py --host 127.0.0.1             # Bind to specific interface
    python main.py --max-workers 100            # Increase SSH concurrency

Architecture:
    main.py
      ├── load_olt_config()        → YAML parsing + defaults resolution
      ├── setup_logging()          → RotatingFileHandler + StreamHandler
      └── SyslogListener.start()   → asyncio UDP server
            ├── OLTRegistry        → O(1) IP lookup
            ├── parse_syslog_message() → Regex parser (Strategy pattern)
            ├── DedupCache         → TTL-based deduplication
            └── ThreadPoolExecutor → ssh_provision_worker()
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import Any

import yaml

from core.listener import DedupCache, OLTRegistry, SyslogListener
from core.ssh_worker import OLTConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = "config/olts.yaml"
DEFAULT_LOG_DIR = "logs"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 514
DEFAULT_MAX_WORKERS = 50

LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(log_dir: str = DEFAULT_LOG_DIR, debug: bool = False) -> None:
    """
    Configure the logging system with dual output:

    - StreamHandler (stdout): INFO level (DEBUG if --debug flag is set).
    - RotatingFileHandler: INFO level, 10 MB per file, 5 backups.

    Log files are stored in the specified directory (created automatically).
    If the log directory or file cannot be created (e.g., permission denied),
    the daemon falls back to console-only logging and continues.

    Args:
        log_dir: Directory for rotating log files.
        debug: If True, set console level to DEBUG.
    """
    log_path = Path(log_dir)
    try:
        log_path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # We'll handle this when trying to create the file handler below
        pass

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all; handlers filter

    # Clear any existing handlers (idempotent)
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(console_handler)

    # Rotating file handler — fall back to console-only on filesystem errors
    log_file = log_path / "olt-provision-daemon.log"
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root_logger.addHandler(file_handler)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        # Log via the already-configured console handler
        logger = logging.getLogger("olt_daemon")
        logger.warning(
            "Cannot write to log file '%s' — %s. "
            "Logging to console only. Fix with: "
            "sudo chown -R olt-daemon:olt-daemon %s",
            log_file, exc, log_path,
        )

    # Suppress noisy third-party loggers
    logging.getLogger("netmiko").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    logger = logging.getLogger("olt_daemon")
    logger.info("Logging initialized — file: %s", log_file)


# =============================================================================
# YAML Config Loading — Defaults Resolution
# =============================================================================

def load_olt_config(config_path: str) -> OLTRegistry:
    """
    Load and validate the multi-OLT configuration from a YAML file.

    Resolution logic:
      1. Read the YAML file.
      2. Extract the 'defaults' section (optional).
      3. For each entry under 'olts', merge OLT-specific values over defaults.
      4. Validate required fields (name, ssh_user, ssh_pass).
      5. Build an OLTRegistry indexed by IP.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        OLTRegistry with all OLTs fully resolved and ready for O(1) IP lookup.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        ValueError: If the YAML is malformed or missing required fields.
    """
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            f"Create it from the template or specify a different path with --config."
        )

    logger = logging.getLogger("olt_daemon")

    with open(config_file, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not raw or "olts" not in raw:
        raise ValueError(
            f"Invalid configuration: '{config_path}' must contain an 'olts' section."
        )

    # Extract defaults section (if present) — unwrap the 'defaults' key
    defaults: dict[str, Any] = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        logger.warning(
            "Invalid 'defaults' section in %s — expected a mapping, got %s. "
            "Using empty defaults.",
            config_path, type(defaults).__name__,
        )
        defaults = {}

    olts_raw = raw["olts"]
    if not isinstance(olts_raw, dict) or not olts_raw:
        raise ValueError(
            f"Configuration error: 'olts' section is empty or not a mapping in {config_path}."
        )

    resolved: dict[str, OLTConfig] = {}

    for ip, olt_data in olts_raw.items():
        if not isinstance(olt_data, dict):
            logger.warning("Skipping invalid OLT entry for IP '%s': not a mapping", ip)
            continue

        # Merge: defaults + OLT-specific values (OLT wins)
        merged = {**defaults, **olt_data}

        # Validate required fields
        missing = []
        for field in ("name", "ssh_user", "ssh_pass"):
            if not merged.get(field):
                missing.append(field)
        if missing:
            logger.error(
                "OLT '%s': missing required fields: %s — skipping",
                ip, ", ".join(missing),
            )
            continue

        # Build OLTConfig with type coercion
        try:
            # Optional internet service-port (gemport 1, e.g. VLAN 600)
            internet_vlan_raw = merged.get("internet_vlan")
            internet_vlan: int | None = None
            internet_gemport: int = 1
            internet_traffic_table_up: str = "7"
            internet_traffic_table_down: str = "7"
            if internet_vlan_raw is not None:
                internet_vlan = int(internet_vlan_raw)
                internet_gemport = int(merged.get("internet_gemport", 1))
                internet_traffic_table_up = str(merged.get("internet_traffic_table_up", "7"))
                internet_traffic_table_down = str(merged.get("internet_traffic_table_down", "7"))

            config = OLTConfig(
                name=str(merged["name"]),
                ip=str(ip),
                ssh_user=str(merged["ssh_user"]),
                ssh_pass=str(merged["ssh_pass"]),
                ssh_port=int(merged.get("ssh_port", 22)),
                parser_model=str(merged.get("parser_model", "huawei_ma5800")),
                management_vlan=int(merged.get("management_vlan", 150)),
                gemport=int(merged.get("gemport", 2)),
                traffic_table_up=str(merged.get("traffic_table_up", "7")),
                traffic_table_down=str(merged.get("traffic_table_down", "7")),
                tr069_profile_id=int(merged.get("tr069_profile_id", 1)),
                dhcp_priority=int(merged.get("dhcp_priority", 2)),
                ip_index=int(merged.get("ip_index", 0)),
                cmd_delay=float(merged.get("cmd_delay", 0.4)),
                max_retries=int(merged.get("max_retries", 10)),
                dedup_ttl_seconds=int(merged.get("dedup_ttl_seconds", 300)),
                internet_vlan=internet_vlan,
                internet_gemport=internet_gemport,
                internet_traffic_table_up=internet_traffic_table_up,
                internet_traffic_table_down=internet_traffic_table_down,
            )
            resolved[ip] = config
            internet_info = ""
            if config.internet_vlan is not None:
                internet_info = (
                    f" internet_vlan={config.internet_vlan}"
                    f" internet_gemport={config.internet_gemport}"
                )
            logger.info(
                "Loaded OLT: %s (%s) — user=%s port=%d vlan=%d tr069_profile=%d%s",
                config.name, config.ip, config.ssh_user,
                config.ssh_port, config.management_vlan,
                config.tr069_profile_id, internet_info,
            )
        except (ValueError, TypeError) as exc:
            logger.error(
                "OLT '%s' (%s): invalid configuration value — %s. Skipping.",
                merged.get("name", "unknown"), ip, exc,
            )

    if not resolved:
        raise ValueError(
            f"No valid OLT configurations loaded from {config_path}. "
            f"Check the file format and required fields."
        )

    logger.info("Loaded %d OLT(s) from %s", len(resolved), config_path)
    return OLTRegistry(resolved)


# =============================================================================
# CLI Argument Parsing
# =============================================================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="OLT Auto-Provision Daemon — Zero-Touch GPON Provisioning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --port 514\n"
            "  python main.py --config /etc/olt/olts.yaml --debug\n"
        ),
    )
    parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the OLT configuration YAML file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--host", "-H",
        default=DEFAULT_HOST,
        help=f"IP address to bind the UDP listener (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"UDP port to listen for syslog messages (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--max-workers", "-w",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Maximum concurrent SSH worker threads (default: {DEFAULT_MAX_WORKERS})",
    )
    parser.add_argument(
        "--log-dir", "-l",
        default=DEFAULT_LOG_DIR,
        help=f"Directory for rotating log files (default: {DEFAULT_LOG_DIR})",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable DEBUG-level logging to console and file",
    )
    return parser.parse_args(argv)


# =============================================================================
# Main Entry Point
# =============================================================================

async def _run_listener(args: argparse.Namespace) -> None:
    """
    Initialize and start the SyslogListener.

    Args:
        args: Parsed command-line arguments.
    """
    logger = logging.getLogger("olt_daemon")

    # Load OLT registry
    logger.info("Loading OLT configuration from: %s", args.config)
    registry = load_olt_config(args.config)

    # Determine dedup TTL from the first OLT's config (or default 300s)
    first_olt = registry.lookup(registry.ips[0]) if registry.ips else None
    dedup_ttl = first_olt.dedup_ttl_seconds if first_olt else 300
    dedup_cache = DedupCache(ttl_seconds=dedup_ttl)

    # Create and start the listener
    listener = SyslogListener(
        host=args.host,
        port=args.port,
        olt_registry=registry,
        dedup_cache=dedup_cache,
        max_workers=args.max_workers,
    )

    # Handle graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal — stopping listener...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Run the listener as a task so we can await the stop event
    listener_task = asyncio.create_task(listener.start())

    # Wait for shutdown signal
    await stop_event.wait()
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    logger.info("Daemon shutdown complete.")


def main(argv: list[str] | None = None) -> None:
    """
    Application entry point.

    Parses CLI arguments, sets up logging, and starts the asyncio event loop
    with the SyslogListener.
    """
    args = parse_args(argv)

    # Setup logging first so all modules can use it
    setup_logging(log_dir=args.log_dir, debug=args.debug)

    logger = logging.getLogger("olt_daemon")
    logger.info("=" * 60)
    logger.info("OLT Auto-Provision Daemon v%s starting...", "1.0.0")
    logger.info("Configuration: %s", os.path.abspath(args.config))
    logger.info("Listener: %s:%d (UDP)", args.host, args.port)
    logger.info("Max SSH workers: %d", args.max_workers)
    logger.info("=" * 60)

    try:
        asyncio.run(_run_listener(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except PermissionError as exc:
        logger.critical(
            "Permission denied binding to %s:%d — %s. "
            "Use a port >= 1024, run as root, or add CAP_NET_BIND_SERVICE capability.",
            args.host, args.port, exc,
        )
        sys.exit(1)
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
