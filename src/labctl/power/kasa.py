"""
Kasa power controller.

Controls TP-Link Kasa smart plugs and power strips via python-kasa library.
Supports single-outlet plugs and multi-outlet strips (e.g., HS300, KP303, EP40).

Per-host state cache
====================

Each multi-outlet strip exposes one KLAP endpoint regardless of how
many outlets it has. Naively probing each outlet independently fans
out into N parallel KLAP handshakes against the same physical device,
which the strip rate-limits — observed in production as bursts of
``AuthenticationError`` against a single IP across all outlets in a
~3 ms window.

This module caches the *state* (channel → is_on) per host with a
short TTL and serializes refreshes through a per-host lock. The first
caller in a cycle pays the KLAP cost; concurrent callers on the same
host wait briefly and read from the cache. Writes invalidate.
"""

import asyncio
import logging
import threading
import time
from functools import lru_cache

from labctl.core.config import load_config
from labctl.power.base import PowerController, PowerState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-host state cache
# ---------------------------------------------------------------------------

# Default TTL for cached states. Tuned to span a single monitor
# `check_all` parallel batch (typically completes in <1 s) without
# letting external state changes go unnoticed for long. CLI/MCP fresh
# processes start with empty caches, so this only matters within the
# daemon and within long-running web requests.
KASA_STATE_TTL_SECONDS = 5.0

_kasa_state_cache: dict[str, tuple[float, dict[int, bool]]] = {}
_kasa_host_locks: dict[str, threading.Lock] = {}
_kasa_cache_meta_lock = threading.Lock()


def _get_host_lock(host: str) -> threading.Lock:
    """Return (lazily creating) the per-host serialization lock."""
    with _kasa_cache_meta_lock:
        lock = _kasa_host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _kasa_host_locks[host] = lock
        return lock


def _read_cached_state(host: str, channel: int) -> bool | None:
    """Return cached is_on for (host, channel), or None if missing/stale."""
    with _kasa_cache_meta_lock:
        entry = _kasa_state_cache.get(host)
        if entry is None:
            return None
        cached_at, states = entry
        if time.monotonic() - cached_at > KASA_STATE_TTL_SECONDS:
            return None
        return states.get(channel)


def _store_host_state(host: str, states: dict[int, bool]) -> None:
    with _kasa_cache_meta_lock:
        _kasa_state_cache[host] = (time.monotonic(), dict(states))


def _invalidate_host(host: str) -> None:
    """Drop the cache entry for a host so the next read refreshes."""
    with _kasa_cache_meta_lock:
        _kasa_state_cache.pop(host, None)


def _clear_kasa_caches() -> None:
    """Reset all per-host caches. Test-only entry point."""
    with _kasa_cache_meta_lock:
        _kasa_state_cache.clear()
        _kasa_host_locks.clear()


@lru_cache(maxsize=1)
def _get_cached_kasa_credentials():
    """Load Kasa credentials once per process."""
    try:
        from kasa import Credentials

        config = load_config()
        if config.kasa.username and config.kasa.password:
            logger.debug("Loaded Kasa credentials for user: %s", config.kasa.username)
            return Credentials(config.kasa.username, config.kasa.password)
        logger.debug("No Kasa credentials configured")
    except Exception as e:
        logger.warning("Failed to load Kasa credentials: %s", e)
    return None


class KasaController(PowerController):
    """
    Power controller for TP-Link Kasa devices.

    Requires python-kasa package: pip install python-kasa

    Supports both single plugs (HS103, EP10, etc.) and multi-outlet
    power strips (HS300, KP303, EP40, etc.). For strips, use plug_index
    to select the specific outlet (1-based).

    Newer Kasa devices using the KLAP protocol require TP-Link cloud
    account credentials. Configure these in config.yaml under the
    'kasa' section.
    """

    def _load_credentials(self):
        """Load Kasa credentials from config, returning Credentials or None."""
        return _get_cached_kasa_credentials()

    async def _get_device(self):
        """
        Connect to device and return (root_device, target) tuple.

        root_device is the top-level device (needed for disconnect cleanup).
        target is the specific outlet to control — either the root device
        itself (for single plugs) or a child device (for strip outlets).

        On failure, ensures the device is disconnected before raising.
        """
        from kasa import Discover

        credentials = self._load_credentials()
        kwargs = {"host": self.address}
        if credentials:
            kwargs["credentials"] = credentials

        logger.debug("Discovering Kasa device at %s", self.address)
        device = await Discover.discover_single(**kwargs)

        try:
            logger.debug(
                "Updating device state for %s (%s)", self.address, device.alias
            )
            await device.update()
        except Exception:
            await device.disconnect()
            raise

        if device.children:
            idx = self.plug_index - 1  # Convert 1-based to 0-based
            if idx < 0 or idx >= len(device.children):
                await device.disconnect()
                raise RuntimeError(
                    f"Outlet index {self.plug_index} out of range "
                    f"(device has {len(device.children)} outlets)"
                )
            target = device.children[idx]
            logger.debug(
                "Selected outlet %d (%s) on strip %s",
                self.plug_index,
                target.alias,
                device.alias,
            )
            return device, target

        return device, device

    def _run_async(self, coro_factory, action: str, retries: int = 2):
        """
        Execute an async coro factory with retry/logging.

        The *factory* pattern (rather than passing the coro directly)
        ensures each retry attempt creates a fresh awaitable — async
        coros are single-shot.

        Retries on authentication / transient errors, which occur
        intermittently with HS300 firmware using the KLAP protocol.

        Logging policy:
          - Retry attempts log at WARNING so they show in default
            (INFO) logs — evidence of intermittent KLAP issues we want
            visible without enabling debug.
          - A successful retry logs at INFO so the recovery is visible.
          - Final failure logs at ERROR (in addition to raising), so
            the full attempt history is captured even when the caller
            only surfaces the exception text.
        """
        last_error = None
        total_attempts = 1 + retries
        for attempt in range(total_attempts):
            try:
                # If already inside an async event loop (e.g., MCP server),
                # run in a new thread to avoid "cannot call asyncio.run()
                # from a running event loop" error.
                try:
                    asyncio.get_running_loop()
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, coro_factory())
                        result = future.result(timeout=self.timeout + 10)
                except RuntimeError:
                    result = asyncio.run(coro_factory())

                if attempt > 0:
                    logger.info(
                        "Kasa %s for %s[%d] succeeded on attempt %d/%d",
                        action,
                        self.address,
                        self.plug_index,
                        attempt + 1,
                        total_attempts,
                    )
                return result
            except ImportError:
                raise RuntimeError(
                    "python-kasa not installed. Install with: pip install python-kasa"
                )
            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                if attempt < retries:
                    logger.warning(
                        "Kasa %s attempt %d/%d failed for %s[%d]: %s: %s — retrying in 2s",
                        action,
                        attempt + 1,
                        total_attempts,
                        self.address,
                        self.plug_index,
                        type(e).__name__,
                        e,
                    )
                    time.sleep(2)
                    continue
                break

        error_type = type(last_error).__name__
        msg = (
            f"Kasa {action} failed for {self.address} "
            f"[{self.plug_index}] after {total_attempts} attempts: "
            f"{error_type}: {last_error}"
        )
        logger.error(msg)
        raise RuntimeError(msg) from last_error

    def _run(self, coro_func, action: str, retries: int = 2):
        """Per-outlet flow used by writes — discovers, runs, disconnects."""

        def factory():
            async def go():
                device, target = await self._get_device()
                try:
                    return await coro_func(device, target)
                finally:
                    await device.disconnect()

            return go()

        return self._run_async(factory, action, retries)

    def _fetch_all_outlet_states(self) -> dict[int, bool]:
        """Discover device, update once, return is_on for every outlet.

        One KLAP handshake covers every child outlet — caller stores
        the result in the host cache so subsequent get_state() calls
        on sibling outlets hit the cache instead of re-handshaking.
        """

        def factory():
            async def go():
                from kasa import Discover

                credentials = self._load_credentials()
                kwargs = {"host": self.address}
                if credentials:
                    kwargs["credentials"] = credentials
                logger.debug("Discovering Kasa device at %s", self.address)
                device = await Discover.discover_single(**kwargs)
                try:
                    await device.update()
                    states: dict[int, bool] = {}
                    if device.children:
                        for i, child in enumerate(device.children, start=1):
                            states[i] = bool(child.is_on)
                    else:
                        states[1] = bool(device.is_on)
                    return states
                finally:
                    await device.disconnect()

            return go()

        return self._run_async(factory, "get_state", retries=2)

    def power_on(self) -> bool:
        """Turn power on."""
        logger.debug("Kasa power_on: %s[%d]", self.address, self.plug_index)

        async def _on(device, target):
            await target.turn_on()
            logger.debug("Power ON sent to %s[%d]", self.address, self.plug_index)
            return True

        # Serialize with concurrent reads on the same host and invalidate
        # so the next get_state() reflects the change.
        with _get_host_lock(self.address):
            try:
                return self._run(_on, "power_on")
            finally:
                _invalidate_host(self.address)

    def power_off(self) -> bool:
        """Turn power off."""
        logger.debug("Kasa power_off: %s[%d]", self.address, self.plug_index)

        async def _off(device, target):
            await target.turn_off()
            logger.debug("Power OFF sent to %s[%d]", self.address, self.plug_index)
            return True

        with _get_host_lock(self.address):
            try:
                return self._run(_off, "power_off")
            finally:
                _invalidate_host(self.address)

    def get_state(self) -> PowerState:
        """Get current power state, using the per-host cache when fresh.

        Concurrent callers on the same host serialize through a per-host
        lock so the strip sees one KLAP handshake per cache window
        instead of one per outlet. This eliminates the parallel-handshake
        contention that the strip rate-limits as auth failures.
        """
        logger.debug("Kasa get_state: %s[%d]", self.address, self.plug_index)

        cached = _read_cached_state(self.address, self.plug_index)
        if cached is not None:
            return PowerState.ON if cached else PowerState.OFF

        with _get_host_lock(self.address):
            # Double-check after acquiring the lock — another caller may
            # have just populated the cache while we were waiting.
            cached = _read_cached_state(self.address, self.plug_index)
            if cached is not None:
                return PowerState.ON if cached else PowerState.OFF

            states = self._fetch_all_outlet_states()
            _store_host_state(self.address, states)

        is_on = states.get(self.plug_index)
        if is_on is None:
            return PowerState.UNKNOWN
        return PowerState.ON if is_on else PowerState.OFF
