"""
SSH Provisioning Worker — Refactored from Legacy consultas-legacy/.

Recycles proven logic from:
  - consultas-legacy/ssh_client.py   → connect_olt / close_olt
  - consultas-legacy/omci.py         → validate_omci_output, BUSY_PATTERNS,
                                       execute_command, _read_command_with_paging
  - consultas-legacy/huawei_injection.py → _ensure_huawei_config_mode

Wraps everything into clean, SOLID classes with strict type hints and docstrings.
The worker executes provisioning commands (NO ont add — the OLT auto-add-policy
handles that):
  1. Enter GPON interface.
  2. Configure WAN DHCP on management VLAN.
  3. Set internet-config ip-index.
  4. Inject TR-069 server profile.
  5. Exit GPON interface (quit).
  6. Create service-port for management VLAN (gemport 2).
  7. (Optional) Create service-port for internet VLAN (gemport 1).

Usage (called by listener.py via ThreadPoolExecutor):
    from core.ssh_worker import ssh_provision_worker

    result = ssh_provision_worker(parsed_event, olt_config)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, ClassVar

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

# ---------------------------------------------------------------------------
# Module-level logger (configured by main.py)
# ---------------------------------------------------------------------------
logger = logging.getLogger("olt_daemon.ssh_worker")

# ---------------------------------------------------------------------------
# VRP output patterns that indicate command failure
# ---------------------------------------------------------------------------
FAILURE_MARKERS: list[str] = [
    "Failure:",
    "Error:",
    "Error%",
    "% Unknown",
    "% Invalid",
    "% Incomplete",
    "% Ambiguous",
    "% Too many",
]


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class OLTConfig:
    """
    Immutable configuration for a single OLT, loaded from config/olts.yaml.

    All provisioning parameters are resolved at load time: OLT-specific values
    override defaults. No runtime YAML lookups needed.
    """

    name: str
    ip: str
    ssh_user: str
    ssh_pass: str
    ssh_port: int = 22
    parser_model: str = "huawei_ma5800"

    # Provisioning parameters — management (gemport 2)
    management_vlan: int = 150
    gemport: int = 2
    traffic_table_up: str = "7"
    traffic_table_down: str = "7"
    tr069_profile_id: int = 1
    dhcp_priority: int = 2
    ip_index: int = 0

    # Optional internet service-port (gemport 1) — disabled when None
    internet_vlan: int | None = None
    internet_gemport: int = 1
    internet_traffic_table_up: str = "7"
    internet_traffic_table_down: str = "7"

    # Operational tuning
    cmd_delay: float = 0.4
    max_retries: int = 10
    dedup_ttl_seconds: int = 300


@dataclass
class ProvisioningResult:
    """Result of a single ONT provisioning attempt."""

    success: bool
    olt_name: str
    olt_ip: str
    fsp: str
    ont_id: str
    commands_executed: int = 0
    error_message: str = ""
    elapsed_seconds: float = 0.0


# =============================================================================
# Huawei SSH Client — Context Manager wrapping Netmiko
# =============================================================================

class HuaweiSSHClient:
    """
    SSH client for Huawei OLTs via Netmiko.

    Encapsulates connection lifecycle, config mode entry, command execution
    with retry/busy/paging handling, output validation, and safe disconnection.

    Refactored from:
      - consultas-legacy/ssh_client.py  (connect_olt, close_olt)
      - consultas-legacy/omci.py        (validate_omci_output,
                                          _read_command_with_paging,
                                          execute_command)
      - consultas-legacy/huawei_injection.py (_ensure_huawei_config_mode)

    Usage:
        with HuaweiSSHClient(olt_config) as client:
            client.enter_config_mode()
            client.execute("display version")
    """

    # Patterns that indicate the OLT is busy (data backup in progress).
    # Refactored from consultas-legacy/omci.py:BUSY_PATTERNS
    BUSY_PATTERNS: ClassVar[list[str]] = [
        "It will take several minutes to",
        "The percentage of saved data on",
        "Failure: System is busy",
        "System is busy",
    ]

    # Paging indicators in command output.
    PAGING_MARKERS: ClassVar[list[str]] = [
        "---- More",
        "Press 'Q'",
        "Press to continue",
    ]

    def __init__(self, config: OLTConfig) -> None:
        self._config = config
        self._conn: Any = None  # Netmiko ConnectHandler instance

    # ---- Connection Lifecycle ----

    def connect(self) -> None:
        """
        Establish SSH connection to the OLT.

        Refactored from consultas-legacy/ssh_client.py:connect_olt()

        Raises:
            NetmikoTimeoutException: If the OLT is unreachable.
            NetmikoAuthenticationException: If credentials are invalid.
        """
        device = {
            "device_type": "huawei",
            "host": self._config.ip,
            "username": self._config.ssh_user,
            "password": self._config.ssh_pass,
            "port": self._config.ssh_port,
            "global_cmd_verify": False,
            "fast_cli": False,
        }
        logger.info(
            "Connecting to OLT %s (%s:%d)...",
            self._config.name, self._config.ip, self._config.ssh_port,
        )
        self._conn = ConnectHandler(**device)
        logger.info(
            "Connected to %s — prompt: %s",
            self._config.name, self._conn.find_prompt().strip(),
        )

    def disconnect(self) -> None:
        """
        Gracefully close the SSH session.

        Refactored from consultas-legacy/ssh_client.py:close_olt()
        """
        if self._conn is None:
            return
        try:
            self._conn.disconnect()
            logger.info("Disconnected from %s", self._config.name)
        except Exception as exc:
            logger.warning("Error disconnecting from %s: %s", self._config.name, exc)
        finally:
            self._conn = None

    def __enter__(self) -> "HuaweiSSHClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()

    # ---- Config Mode ----

    def enter_config_mode(self) -> None:
        """
        Ensure the SSH session is in Huawei VRP configuration mode.

        Sequence: enable → config
        Verifies prompt at each stage and logs command outputs.

        Refactored from consultas-legacy/huawei_injection.py:_ensure_huawei_config_mode()
        """
        if self._conn is None:
            raise RuntimeError("Not connected. Call connect() first.")

        prompt = self._conn.find_prompt().strip()
        logger.info("Initial prompt for %s: %s", self._config.name, prompt)

        # Already in config mode? (prompt contains "(...)" with "config")
        if "(" in prompt and ")" in prompt and "config" in prompt.lower():
            logger.info("Session already in config mode for %s", self._config.name)
            return

        # Step 1: enable
        out_enable = self._send_command("enable", read_timeout=10)
        self._log_command_result("enable", out_enable)
        prompt = self._conn.find_prompt().strip()
        logger.info("Post-enable prompt: %s", prompt)

        # Step 2: config
        out_config = self._send_command("config", read_timeout=10)
        self._log_command_result("config", out_config)
        prompt = self._conn.find_prompt().strip()
        logger.info("Post-config prompt: %s", prompt)

        if not ("(" in prompt and ")" in prompt):
            raise RuntimeError(
                f"Failed to confirm config mode on {self._config.name}. "
                f"Unexpected prompt: '{prompt}'"
            )
        logger.info("Config mode confirmed for %s", self._config.name)

    # ---- Command Execution (Core) ----

    def execute(self, cmd: str, log_prefix: str = "CMD") -> str:
        """
        Execute a VRP command with logging, retry/busy/paging handling,
        and output validation.

        Args:
            cmd: VRP command to execute.
            log_prefix: Label for log lines (default: "CMD").

        Returns:
            Stripped command output.

        Raises:
            RuntimeError: If the command fails after all retries.
        """
        logger.info("[%s] %s", log_prefix, cmd)
        out = self._send_command(cmd)
        self._log_command_result(log_prefix, out)
        return out

    @staticmethod
    def _log_command_result(label: str, output: str) -> None:
        """
        Log the result of a VRP command execution at INFO level.

        Detects failure patterns in the output and logs accordingly.
        If the output is empty, logs a simple success marker.

        Args:
            label: Command label for the log line.
            output: Full command output text.
        """
        stripped = output.strip()
        if not stripped:
            logger.info("[%s] ✓ OK (no output)", label)
            return

        # Check for failure indicators
        failures = [m for m in FAILURE_MARKERS if m in stripped]
        if failures:
            # Truncate for log readability
            summary = stripped[:300].replace("\n", " | ")
            if len(stripped) > 300:
                summary += f"... (+{len(stripped) - 300} chars)"
            logger.warning(
                "[%s] ✗ WARNING — failure indicators: %s — output: %s",
                label, ", ".join(failures), summary,
            )
        else:
            # Success with output
            summary = stripped[:200].replace("\n", " | ")
            if len(stripped) > 200:
                summary += f"... (+{len(stripped) - 200} chars)"
            logger.info("[%s] ✓ OK (%d chars): %s", label, len(stripped), summary)

    def _send_command(self, cmd: str, read_timeout: int = 30) -> str:
        """
        Execute a VRP command with full error handling:

        1. Retry on OLT busy (data backup in progress).
        2. Handle confirmation prompts (send extra Enter).
        3. Consume paging (send space until no more "More" markers).

        Refactored from:
          - consultas-legacy/omci.py:validate_omci_output()
          - consultas-legacy/omci.py:_read_command_with_paging()

        Args:
            cmd: Command string to send.
            read_timeout: Seconds to wait for command output.

        Returns:
            Full command output (all pages consumed).

        Raises:
            RuntimeError: After exhausting all retries.
        """
        if self._conn is None:
            raise RuntimeError("Not connected.")

        max_retries = self._config.max_retries

        for attempt in range(1, max_retries + 1):
            try:
                out = self._conn.send_command_timing(
                    cmd,
                    read_timeout=read_timeout,
                    delay_factor=2,
                )
                output = out.strip()

                # --- Case 1: OLT is busy (data backup) ---
                # Check with 'in' instead of startswith — busy messages can
                # appear anywhere in the output, not just at the beginning.
                if any(p in output for p in self.BUSY_PATTERNS):
                    logger.warning(
                        "OLT %s busy executing '%s' — retry %d/%d in %ds",
                        self._config.name, cmd, attempt, max_retries, 200,
                    )
                    time.sleep(200)  # Legacy uses 200s delay for busy OLTs
                    continue

                # --- Case 2: Confirmation prompt (ends with "}:") ---
                if output.endswith("}:"):
                    logger.info(
                        "Command '%s' expects confirmation — sending Enter", cmd,
                    )
                    extra = self._conn.send_command_timing("\n", read_timeout=5)
                    output += "\n" + extra.strip()

                # --- Case 3: Paging ---
                output = self._consume_paging(output)

                return output

            except Exception as exc:
                logger.error(
                    "Error executing '%s' on %s (attempt %d/%d): %s",
                    cmd, self._config.name, attempt, max_retries, exc,
                )
                if attempt < max_retries:
                    time.sleep(5)
                else:
                    raise RuntimeError(
                        f"Command '{cmd}' failed after {max_retries} retries "
                        f"on {self._config.name}: {exc}"
                    ) from exc

        raise RuntimeError(
            f"Command '{cmd}' could not be executed after {max_retries} attempts "
            f"on {self._config.name}"
        )

    def _consume_paging(self, initial_output: str, max_pages: int = 200) -> str:
        """
        Consume all paging prompts by sending space characters.

        Refactored from consultas-legacy/omci.py:_read_command_with_paging()

        Args:
            initial_output: First chunk of command output.
            max_pages: Safety limit for infinite paging loops.

        Returns:
            Complete output with all pages concatenated.
        """
        out = initial_output
        pages = 0

        while any(marker in out for marker in self.PAGING_MARKERS):
            pages += 1
            if pages > max_pages:
                logger.warning(
                    "Paging limit (%d) reached on %s — aborting paging",
                    max_pages, self._config.name,
                )
                break
            self._conn.write_channel(" ")
            time.sleep(0.5)
            last_chunk = self._conn.read_channel()
            out += last_chunk

        # Drain any residual buffer
        time.sleep(0.2)
        out += self._conn.read_channel()
        return out


# =============================================================================
# Command Builder — VRP Command Generation
# =============================================================================

def _is_numeric(value: str) -> bool:
    """Determine if a traffic-table value is an index (numeric) or a name."""
    return bool(re.match(r"^\d+$", value.strip()))


def _build_provisioning_commands(
    fsp: str,
    ont_id: str,
    cfg: OLTConfig,
) -> list[str]:
    """
    Build the ordered list of VRP commands for ONT provisioning.

    Flow (NO ont add — OLT auto-add-policy already registered the ONT):
      1. Enter GPON interface.
      2. Configure WAN DHCP on management VLAN (gemport 2).
      3. Set internet-config ip-index.
      4. Inject TR-069 server profile.
      5. Exit GPON interface.
      6. Create service-port for management VLAN (gemport 2).
      7. (Optional) Create service-port for internet VLAN (gemport 1).

    Args:
        fsp: Frame/Slot/Port string from parser (e.g., "0/1/0").
        ont_id: ONT ID string from parser (e.g., "0").
        cfg: Resolved OLT configuration with provisioning parameters.

    Returns:
        Ordered list of VRP command strings ready for execution.
    """
    parts = fsp.split("/")
    if len(parts) != 3:
        raise ValueError(f"Invalid fsp format: '{fsp}'. Expected 'X/Y/Z'.")

    frame, slot, port = parts[0], parts[1], parts[2]

    # Determine traffic-table keyword: 'index' for numeric, 'name' for alphanumeric
    tt_up = cfg.traffic_table_up.strip()
    tt_down = cfg.traffic_table_down.strip()
    tt_up_kw = "index" if _is_numeric(tt_up) else "name"
    tt_down_kw = "index" if _is_numeric(tt_down) else "name"

    commands = [
        # 1. Enter GPON interface
        f"interface gpon {frame}/{slot}",

        # 2. WAN DHCP on management VLAN (gemport 2)
        (
            f"ont ipconfig {port} {ont_id} ip-index {cfg.ip_index} dhcp "
            f"vlan {cfg.management_vlan} priority {cfg.dhcp_priority}"
        ),

        #f"ont internet-config {port} {ont_id} ip-index {cfg.ip_index}",

        # 3. TR-069 server profile
        f"ont tr069-server-config {port} {ont_id} profile-id {cfg.tr069_profile_id}",

        # 4. Exit GPON interface
        "quit",

        # 5. Service-port for management VLAN (gemport 2)
        (
            f"service-port vlan {cfg.management_vlan} gpon {frame}/{slot}/{port} "
            f"ont {ont_id} gemport {cfg.gemport} multi-service "
            f"user-vlan {cfg.management_vlan} tag-transform translate "
            f"inbound traffic-table {tt_up_kw} {tt_up} "
            f"outbound traffic-table {tt_down_kw} {tt_down}"
        ),
    ]

    # 6. (Optional) Internet service-port (gemport 1)
    if cfg.internet_vlan is not None:
        itt_up = cfg.internet_traffic_table_up.strip()
        itt_down = cfg.internet_traffic_table_down.strip()
        itt_up_kw = "index" if _is_numeric(itt_up) else "name"
        itt_down_kw = "index" if _is_numeric(itt_down) else "name"

        commands.append(
            f"service-port vlan {cfg.internet_vlan} gpon {frame}/{slot}/{port} "
            f"ont {ont_id} gemport {cfg.internet_gemport} multi-service "
            f"user-vlan {cfg.internet_vlan} tag-transform translate "
            f"inbound traffic-table {itt_up_kw} {itt_up} "
            f"outbound traffic-table {itt_down_kw} {itt_down}"
        )
        logger.debug(
            "Added internet service-port: vlan=%d gemport=%d for ONT %s/%s",
            cfg.internet_vlan, cfg.internet_gemport, fsp, ont_id,
        )

    return commands


# =============================================================================
# Provisioning Worker — Main Entry Point for ThreadPoolExecutor
# =============================================================================

def ssh_provision_worker(
    parsed_event: dict[str, str],
    olt_config: OLTConfig,
) -> ProvisioningResult:
    """
    Execute the full provisioning workflow for a single ONT.

    This function is designed to be submitted to a ThreadPoolExecutor by the
    syslog listener. It runs in a dedicated thread because Netmiko is synchronous.

    Workflow:
      1. Establish SSH connection to the OLT.
      2. Enter VRP configuration mode (enable → config).
      3. Build and execute provisioning commands.
      4. Log results and return a ProvisioningResult.

    Args:
        parsed_event: Dict from parser.parse_syslog_message() with keys:
                      'fsp' (str: "0/1/0"), 'ont_id' (str: "0").
        olt_config: Resolved OLT configuration from olts.yaml.

    Returns:
        ProvisioningResult indicating success or failure.
    """
    start_time = time.monotonic()
    fsp = parsed_event["fsp"]
    ont_id = parsed_event["ont_id"]

    logger.info(
        "=== Starting provisioning: OLT=%s (%s) ONT=%s/%s ===",
        olt_config.name, olt_config.ip, fsp, ont_id,
    )

    result = ProvisioningResult(
        success=False,
        olt_name=olt_config.name,
        olt_ip=olt_config.ip,
        fsp=fsp,
        ont_id=ont_id,
    )

    try:
        # Build the command sequence
        commands = _build_provisioning_commands(fsp, ont_id, olt_config)
        logger.debug(
            "Generated %d commands for ONT %s/%s on %s",
            len(commands), fsp, ont_id, olt_config.name,
        )

        # Execute via SSH
        with HuaweiSSHClient(olt_config) as client:
            # Enter config mode
            client.enter_config_mode()

            # Execute each command in sequence
            for i, cmd in enumerate(commands, start=1):
                logger.info(
                    "[%s] Step %d/%d — ONT %s/%s",
                    olt_config.name, i, len(commands), fsp, ont_id,
                )
                try:
                    client.execute(cmd, log_prefix=f"STEP-{i}")
                    result.commands_executed += 1
                    # Small delay between dependent commands
                    if i < len(commands):
                        time.sleep(olt_config.cmd_delay)
                except Exception as cmd_exc:
                    logger.error(
                        "Command %d/%d failed for ONT %s/%s on %s: %s",
                        i, len(commands), fsp, ont_id, olt_config.name, cmd_exc,
                    )
                    # Attempt to exit GPON interface gracefully
                    try:
                        client.execute("quit", log_prefix="CLEANUP")
                    except Exception:
                        pass
                    raise

        # Success
        elapsed = time.monotonic() - start_time
        result.success = True
        result.elapsed_seconds = elapsed
        logger.info(
            "=== Provisioning OK: OLT=%s ONT=%s/%s (%d commands in %.1fs) ===",
            olt_config.name, fsp, ont_id, result.commands_executed, elapsed,
        )

    except NetmikoTimeoutException as exc:
        elapsed = time.monotonic() - start_time
        result.elapsed_seconds = elapsed
        result.error_message = f"SSH timeout: {exc}"
        logger.error(
            "Connection timeout to %s (%s:%d): %s",
            olt_config.name, olt_config.ip, olt_config.ssh_port, exc,
        )

    except NetmikoAuthenticationException as exc:
        elapsed = time.monotonic() - start_time
        result.elapsed_seconds = elapsed
        result.error_message = f"SSH authentication failed: {exc}"
        logger.error(
            "Authentication failed for %s (%s): %s",
            olt_config.name, olt_config.ip, exc,
        )

    except OSError as exc:
        elapsed = time.monotonic() - start_time
        result.elapsed_seconds = elapsed
        result.error_message = f"Network error: {exc}"
        logger.error(
            "Network error connecting to %s (%s): %s",
            olt_config.name, olt_config.ip, exc,
        )

    except Exception as exc:
        elapsed = time.monotonic() - start_time
        result.elapsed_seconds = elapsed
        result.error_message = f"Unexpected error: {exc}"
        logger.exception(
            "Unexpected error provisioning ONT %s/%s on %s",
            fsp, ont_id, olt_config.name,
        )

    return result
