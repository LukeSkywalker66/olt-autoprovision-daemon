"""
Syslog UDP Listener — Async Server with ThreadPoolExecutor for SSH Workers.

Listens for syslog datagrams on a configurable UDP port. For each datagram:
  1. Extracts the source IP (OLT identity) from the UDP packet address.
  2. Looks up the OLT configuration in the multi-OLT registry.
  3. Parses the syslog message using the OLT's configured parser model.
  4. If the message is a relevant event (ONT auto-discovered), checks the
     deduplication cache to avoid reprocessing duplicate events.
  5. Submits the SSH provisioning worker to a ThreadPoolExecutor.

Concurrency model:
  - asyncio: UDP receive loop (non-blocking, high throughput).
  - ThreadPoolExecutor: SSH workers (Netmiko is synchronous/blocking).
  - In-memory TTL cache: deduplication of repeated syslog events.

Architecture:
  Listener (asyncio) ──submit──> ThreadPoolExecutor ──run──> ssh_provision_worker()
       │                              (max 50 threads)
       └── parse_syslog_message() ── cache check ── submit or skip
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core.parser import parse_syslog_message
from core.ssh_worker import OLTConfig, ProvisioningResult, ssh_provision_worker

# ---------------------------------------------------------------------------
# Module-level logger (configured by main.py)
# ---------------------------------------------------------------------------
logger = logging.getLogger("olt_daemon.listener")


# =============================================================================
# Deduplication Cache — TTL-based in-memory store
# =============================================================================

class DedupCache:
    """
    Simple in-memory TTL cache for event deduplication.

    Prevents the same (olt_ip, fsp, ont_id) tuple from being provisioned
    multiple times within the TTL window. This handles the case where the OLT
    sends the same "service port incorrect" warning multiple times for the
    same ONT discovery event.

    Thread-safe for use across asyncio and ThreadPoolExecutor contexts.
    Uses a plain dict with periodic or lazy eviction of expired entries.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        """
        Args:
            ttl_seconds: Time-to-live in seconds for cache entries (default: 300 = 5 min).
        """
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str, str], float] = {}

    def is_duplicate(self, olt_ip: str, fsp: str, ont_id: str) -> bool:
        """
        Check if an event was already processed recently.

        Also performs lazy eviction of expired entries on every check.

        Args:
            olt_ip: Source IP of the OLT.
            fsp: Frame/Slot/Port string (e.g., "0/1/0").
            ont_id: ONT ID string (e.g., "0").

        Returns:
            True if the event was already seen within the TTL window.
        """
        key = (olt_ip, fsp, ont_id)
        now = time.monotonic()

        # Lazy eviction: remove all expired entries
        expired = [k for k, ts in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]

        if key in self._store:
            age = now - self._store[key]
            logger.debug(
                "Dedup HIT: OLT=%s ONT=%s/%s (age=%.1fs, ttl=%ds)",
                olt_ip, fsp, ont_id, age, self._ttl,
            )
            return True

        # Not a duplicate — record it
        self._store[key] = now
        logger.debug(
            "Dedup MISS: OLT=%s ONT=%s/%s — recorded in cache (ttl=%ds)",
            olt_ip, fsp, ont_id, self._ttl,
        )
        return False

    def __len__(self) -> int:
        """Return the number of active (non-expired) cache entries."""
        now = time.monotonic()
        return sum(1 for ts in self._store.values() if now - ts <= self._ttl)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._store.clear()


# =============================================================================
# OLT Registry — In-memory lookup by IP
# =============================================================================

class OLTRegistry:
    """
    Registry of all known OLTs, indexed by IP address for O(1) lookup.

    Loaded once at startup from config/olts.yaml. Each entry resolves
    defaults so that every OLTConfig is fully populated.
    """

    def __init__(self, olts: dict[str, OLTConfig]) -> None:
        """
        Args:
            olts: Mapping of IP → OLTConfig (already resolved with defaults).
        """
        self._olts = olts

    def lookup(self, ip: str) -> OLTConfig | None:
        """
        Find an OLT by its IP address.

        Args:
            ip: Source IP from the UDP datagram.

        Returns:
            OLTConfig if found, None otherwise.
        """
        return self._olts.get(ip)

    @property
    def count(self) -> int:
        """Number of registered OLTs."""
        return len(self._olts)

    @property
    def ips(self) -> list[str]:
        """List of all registered OLT IPs."""
        return list(self._olts.keys())


# =============================================================================
# Syslog Listener — Asyncio UDP Server
# =============================================================================

class SyslogListener:
    """
    Asynchronous UDP syslog server with ThreadPoolExecutor for SSH workers.

    Listens perpetually on a configurable UDP port. For each received datagram:
      1. Decode the raw message.
      2. Extract the OLT source IP from the socket address.
      3. Look up the OLT in the registry.
      4. Parse the syslog message.
      5. Check deduplication cache.
      6. Submit the SSH worker to the thread pool.

    The listener itself NEVER blocks — all SSH work is offloaded to threads.
    """

    def __init__(
        self,
        host: str,
        port: int,
        olt_registry: OLTRegistry,
        dedup_cache: DedupCache | None = None,
        max_workers: int = 50,
        default_parser_model: str = "huawei_ma5800",
    ) -> None:
        """
        Args:
            host: IP address to bind the UDP socket to (e.g., "0.0.0.0").
            port: UDP port to listen on (e.g., 5514).
            olt_registry: Registry of known OLTs with their configurations.
            dedup_cache: Optional deduplication cache. Created automatically if None.
            max_workers: Maximum number of concurrent SSH worker threads.
            default_parser_model: Fallback parser model if OLT config doesn't specify one.
        """
        self.host = host
        self.port = port
        self.registry = olt_registry
        self.dedup = dedup_cache or DedupCache()
        self.default_parser_model = default_parser_model

        # Thread pool for blocking SSH operations
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ssh-worker",
        )

        # Statistics
        self._datagrams_received: int = 0
        self._events_matched: int = 0
        self._events_duplicated: int = 0
        self._workers_submitted: int = 0

    # ---- Public API ----

    async def start(self) -> None:
        """
        Start the perpetual UDP listen loop.

        This method never returns under normal operation. It can be cancelled
        via asyncio task cancellation (e.g., SIGTERM handler).
        """
        logger.info(
            "Starting SyslogListener on %s:%d (max_workers=%d, registered_olts=%d)",
            self.host, self.port, self._executor._max_workers, self.registry.count,
        )
        logger.info("Registered OLT IPs: %s", ", ".join(self.registry.ips))

        loop = asyncio.get_running_loop()

        # Create the UDP datagram endpoint
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _SyslogProtocol(self._on_datagram),
            local_addr=(self.host, self.port),
        )

        logger.info("SyslogListener is now listening on UDP :%d", self.port)

        try:
            # Keep the listener alive indefinitely
            while True:
                await asyncio.sleep(3600)  # Sleep 1h; wake to keep task alive
        except asyncio.CancelledError:
            logger.info("SyslogListener received shutdown signal")
        finally:
            transport.close()
            self._executor.shutdown(wait=True, cancel_futures=False)
            logger.info(
                "SyslogListener stopped. Stats: received=%d matched=%d "
                "duplicated=%d submitted=%d",
                self._datagrams_received, self._events_matched,
                self._events_duplicated, self._workers_submitted,
            )

    # ---- Internal: Datagram Handler ----

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Callback invoked by the asyncio UDP protocol for each received datagram.

        This runs on the asyncio event loop — it MUST be fast and non-blocking.
        Heavy work (SSH) is offloaded to the ThreadPoolExecutor.

        Args:
            data: Raw bytes of the syslog message.
            addr: (source_ip, source_port) tuple from the UDP packet.
        """
        self._datagrams_received += 1
        src_ip = addr[0]

        # ---- Step 1: Decode the raw message ----
        try:
            raw_message = data.decode("utf-8", errors="replace")
        except Exception:
            raw_message = data.decode("latin-1", errors="replace")

        logger.debug(
            "Datagram #%d from %s:%d (%d bytes)",
            self._datagrams_received, src_ip, addr[1], len(data),
        )

        # ---- Step 2: Lookup OLT by source IP ----
        olt_config = self.registry.lookup(src_ip)
        if olt_config is None:
            logger.warning(
                "Unknown OLT source IP: %s — discarding datagram. "
                "Add this IP to config/olts.yaml to enable provisioning.",
                src_ip,
            )
            return

        # ---- Step 3: Parse the syslog message ----
        parser_model = olt_config.parser_model or self.default_parser_model
        parsed = parse_syslog_message(raw_message, model=parser_model)
        if parsed is None:
            # Not a relevant event — normal, most syslog messages are unrelated
            logger.debug(
                "Non-matching syslog from %s (%s) — discarded",
                olt_config.name, src_ip,
            )
            return

        self._events_matched += 1
        fsp = parsed["fsp"]
        ont_id = parsed["ont_id"]

        logger.info(
            "✓ ONT auto-discovered: OLT=%s (%s) FSP=%s ONT_ID=%s",
            olt_config.name, src_ip, fsp, ont_id,
        )

        # ---- Step 4: Deduplication check ----
        if self.dedup.is_duplicate(src_ip, fsp, ont_id):
            self._events_duplicated += 1
            logger.info(
                "Dedup SKIP: OLT=%s ONT=%s/%s already processed recently",
                olt_config.name, fsp, ont_id,
            )
            return

        # ---- Step 5: Submit SSH worker to thread pool ----
        self._workers_submitted += 1
        future = self._executor.submit(
            ssh_provision_worker,
            parsed_event=parsed,
            olt_config=olt_config,
        )
        future.add_done_callback(
            lambda fut: self._on_worker_done(fut, olt_config.name, fsp, ont_id)
        )

    @staticmethod
    def _on_worker_done(
        future: asyncio.Future,
        olt_name: str,
        fsp: str,
        ont_id: str,
    ) -> None:
        """
        Callback invoked when a ThreadPoolExecutor worker completes.

        Logs the result. Runs in a thread pool thread (not the asyncio loop),
        so logging is the only safe operation here.

        Args:
            future: Completed Future containing a ProvisioningResult.
            olt_name: OLT name for logging context.
            fsp: Frame/Slot/Port for logging context.
            ont_id: ONT ID for logging context.
        """
        try:
            result: ProvisioningResult = future.result()
            if result.success:
                logger.info(
                    "✓ Worker OK: OLT=%s ONT=%s/%s (%d cmds in %.1fs)",
                    olt_name, fsp, ont_id,
                    result.commands_executed, result.elapsed_seconds,
                )
            else:
                logger.error(
                    "✗ Worker FAIL: OLT=%s ONT=%s/%s — %s",
                    olt_name, fsp, ont_id, result.error_message,
                )
        except Exception as exc:
            logger.exception(
                "Worker exception for OLT=%s ONT=%s/%s: %s",
                olt_name, fsp, ont_id, exc,
            )


# =============================================================================
# Asyncio UDP Protocol — Thin adapter
# =============================================================================

class _SyslogProtocol(asyncio.DatagramProtocol):
    """
    Minimal asyncio DatagramProtocol adapter.

    Delegates every received datagram to a synchronous callback.
    The callback MUST be non-blocking (the listener offloads SSH to threads).
    """

    def __init__(self, callback: Callable[[bytes, tuple[str, int]], None]) -> None:
        super().__init__()
        self._callback = callback

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Called by asyncio when a UDP datagram arrives."""
        try:
            self._callback(data, addr)
        except Exception:
            logger.exception("Unhandled error in datagram callback for %s", addr)
