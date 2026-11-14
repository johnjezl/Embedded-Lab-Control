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
import importlib
import logging
import re
import sys
import threading
import time
import zoneinfo
from functools import lru_cache

from labctl.core.config import load_config
from labctl.extras import DIST_NAME, BrokenExtraError, MissingExtraError, is_missing
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
        _missing_zones.clear()


# ---------------------------------------------------------------------------
# Missing time zones (#76)
# ---------------------------------------------------------------------------
#
# A device whose time zone this host can't load (see _missing_zone_message)
# fails every call the same way, after a discovery and a KLAP handshake:
# the monitor checking a strip's six outlets would handshake six times and
# log six identical errors per cycle, and strips rate-limit handshakes.
# So the failure is remembered per device (another device may be in a zone
# the host has): later calls fail at once, without contacting it, and the
# error is logged once. Each call first checks locally whether the zone
# loads now (tzdata installed since, no restart needed), and every
# MISSING_ZONE_RECHECK_SECONDS one call contacts the device anyway, in case
# its time zone setting was changed.

MISSING_ZONE_RECHECK_SECONDS = 600.0

# Host address -> [zone key or None, error text, monotonic time of the last
# call that contacted the device]. Guarded by _kasa_cache_meta_lock.
_missing_zones: dict[str, list] = {}

_ZONE_KEY = re.compile(r"key (\S+)")


def _zone_key(detail: str) -> str | None:
    """The zone named in a ZoneInfoNotFoundError's text ("No time zone
    found with key PST8PDT"), or None if it isn't in that form."""
    match = _ZONE_KEY.search(detail)
    return match.group(1) if match else None


def _zone_loads(zone: str) -> bool:
    """True if this host can load ``zone`` now (no cache: a failed load
    isn't cached by zoneinfo, and a fresh one proves the data is there)."""
    importlib.invalidate_caches()  # a tzdata package installed meanwhile
    try:
        zoneinfo.ZoneInfo.no_cache(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _record_missing_zone(host: str, detail: str) -> bool:
    """Remember that ``host``'s zone is missing; True the first time
    (for this host and zone), so the error is logged once."""
    zone = _zone_key(detail)
    with _kasa_cache_meta_lock:
        known = _missing_zones.get(host)
        _missing_zones[host] = [zone, detail, time.monotonic()]
    return known is None or known[0] != zone


def _known_missing_zone(host: str) -> str | None:
    """The error text if calls to ``host`` should fail at once (its zone
    is still missing and the recheck isn't due), else None. When the
    recheck is due, the first caller to ask goes to the device (the time
    is reset under the lock, so concurrent callers still fail at once)."""
    with _kasa_cache_meta_lock:
        known = _missing_zones.get(host)
    if known is None:
        return None
    zone, detail, _ = known
    if zone is not None and _zone_loads(zone):
        with _kasa_cache_meta_lock:
            _missing_zones.pop(host, None)
        logger.info("Kasa: time zone %s loads now; contacting %s again", zone, host)
        return None
    with _kasa_cache_meta_lock:
        known = _missing_zones.get(host)
        if known is None:
            return None
        if time.monotonic() - known[2] >= MISSING_ZONE_RECHECK_SECONDS:
            known[2] = time.monotonic()
            return None
        return known[1]


def _forget_missing_zone(host: str) -> None:
    """A call to ``host`` succeeded: whatever its zone was, it loads."""
    with _kasa_cache_meta_lock:
        known = _missing_zones.pop(host, None)
    if known is not None:
        logger.info("Kasa: %s works again (its time zone loads)", host)


# First python-kasa release that uses KLAP v2 for IOT devices whose
# discovery reports login version 2 (e.g. HS300 firmware 1.1.2); older
# releases always use KLAP v1 and fail those devices' handshake.
KASA_KLAP_V2_VERSION = (0, 12)


def _klap_v2_hint() -> str:
    """Hint appended to an AuthenticationError from python-kasa < 0.12."""
    try:
        from importlib.metadata import version

        installed = version("python-kasa")
        major_minor = tuple(int(p) for p in installed.split(".")[:2])
    except Exception:
        return ""
    if major_minor >= KASA_KLAP_V2_VERSION:
        return ""
    return (
        f" (python-kasa {installed} can't authenticate to devices on KLAP "
        "login version 2, e.g. HS300 firmware 1.1.2; if the credentials are "
        "correct, upgrade to python-kasa >= 0.12, which needs Python >= 3.11)"
    )


@lru_cache(maxsize=1)
def _get_cached_kasa_credentials():
    """Load Kasa credentials once per process.

    A missing python-kasa raises ModuleNotFoundError instead of returning
    None: lru_cache doesn't cache exceptions, so the outcome isn't fixed
    for the life of the process.
    """
    try:
        from kasa import Credentials

        config = load_config()
        if config.kasa.username and config.kasa.password:
            logger.debug("Loaded Kasa credentials for user: %s", config.kasa.username)
            return Credentials(config.kasa.username, config.kasa.password)
        logger.debug("No Kasa credentials configured")
    except Exception as e:
        if isinstance(e, ImportError) and is_missing(e, "kasa"):
            raise
        logger.warning("Failed to load Kasa credentials: %s", e)
    return None


def _missing_zone_message(action: str, address: str, detail: str) -> str:
    """python-kasa 0.7.7 (Python 3.10) loads the device's time zone by a
    legacy name such as PST8PDT. Hosts without those names (Ubuntu 24.04+
    moved them to tzdata-legacy) need the tzdata package, which the [kasa]
    extra installs on 3.10; an install made before it did may lack it. A
    host with no time zone database at all needs it on any Python.

    The hint installs tzdata alone: reinstalling labctl with the extra
    would replace a checkout install (as the service scripts make) with
    the PyPI release, and on 3.11+ the extra doesn't include tzdata. It
    names the interpreter running labctl (a service venv, say): a bare
    ``pip`` may be another Python's, or refuse (PEP 668). A service venv
    belongs to root, hence the note on who runs it."""
    python = sys.executable or "python3"
    return (
        f"Kasa {action} failed for {address}: this system has no time zone "
        f"data for the device's zone ({detail}). Install the tzdata package "
        f"into labctl's environment, as its owner (e.g. with sudo): "
        f"{python} -m pip install tzdata "
        f"(pipx: pipx inject {DIST_NAME} tzdata)"
    )


class KasaController(PowerController):
    """
    Power controller for TP-Link Kasa devices.

    Requires python-kasa (the [kasa] extra).

    Supports both single plugs (HS103, EP10, etc.) and multi-outlet
    power strips (HS300, KP303, EP40, etc.). For strips, use plug_index
    to select the specific outlet (1-based).

    Newer Kasa devices using the KLAP protocol require TP-Link cloud
    account credentials. Configure these in config.yaml under the
    'kasa' section.
    """

    def _load_credentials(self):
        """Load Kasa credentials from config, returning Credentials or None."""
        try:
            return _get_cached_kasa_credentials()
        except ModuleNotFoundError:
            # python-kasa is missing; _run_async reports the [kasa] extra.
            return None

    async def _discover_and_update(self):
        """Discover the device at self.address and fetch its state.

        Discovery over KLAP (UDP 20002) types an IOT device by its family
        alone: ``IOT.SMARTPLUGSWITCH`` becomes ``IotPlug``, so a strip
        that no longer answers legacy discovery (HS300 firmware 1.1.2
        closed port 9999) comes back as a plug without children. For IOT
        devices this does what python-kasa's own ``connect`` does: query
        the sysinfo, let ``get_device_class_from_sys_info`` pick the
        class, and rebuild the device as that class on the same protocol
        (same session, no new handshake) when discovery picked another.

        The caller must disconnect the returned device; on failure
        (including cancellation) it is disconnected here.
        """
        from kasa import Discover
        from kasa.device_factory import (
            GET_SYSINFO_QUERY,
            get_device_class_from_sys_info,
        )
        from kasa.iot import IotDevice

        credentials = self._load_credentials()
        kwargs = {"host": self.address}
        if credentials:
            kwargs["credentials"] = credentials

        logger.debug("Discovering Kasa device at %s", self.address)
        device = await Discover.discover_single(**kwargs)

        try:
            # Legacy discovery (port 9999) already returns the sysinfo and
            # picks the class from it; only KLAP discovery needs the query.
            discovery_info = getattr(device, "_discovery_info", None) or {}
            from_sysinfo = bool(discovery_info.get("system", {}).get("get_sysinfo"))
            if isinstance(device, IotDevice) and not from_sysinfo:
                info = await device.protocol.query(GET_SYSINFO_QUERY)
                device_class = get_device_class_from_sys_info(info)
                if type(device) is not device_class:
                    logger.debug(
                        "Kasa device at %s discovered as %s; using %s",
                        self.address,
                        type(device).__name__,
                        device_class.__name__,
                    )
                    device = device_class(self.address, protocol=device.protocol)
                    device.update_from_discover_info(info)
            logger.debug(
                "Updating device state for %s (%s)", self.address, device.alias
            )
            await device.update()
        except BaseException:
            await device.disconnect()
            raise
        return device

    async def _get_device(self):
        """
        Connect to device and return (root_device, target) tuple.

        root_device is the top-level device (needed for disconnect cleanup).
        target is the specific outlet to control — either the root device
        itself (for single plugs) or a child device (for strip outlets).

        On failure, ensures the device is disconnected before raising.
        """
        device = await self._discover_and_update()

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

        # No outlets: only index 1 (or the legacy 0) means the device
        # itself. Anything else is a misconfiguration or a strip that
        # wasn't recognised; switching the root would switch every outlet.
        if self.plug_index > 1:
            await device.disconnect()
            raise RuntimeError(
                f"Outlet index {self.plug_index} given, but the Kasa device "
                f"at {self.address} reports no outlets"
            )
        return device, device

    def _run_async(self, coro_factory, action: str, retries: int = 2):
        """
        Execute an async coro factory with retry/logging.

        The *factory* pattern (rather than passing the coro directly)
        ensures each retry attempt creates a fresh awaitable — async
        coros are single-shot.

        Retries on authentication / transient errors (e.g. a strip
        refusing a burst of concurrent handshakes). An authentication
        error that persists with python-kasa < 0.12 is usually the
        device needing KLAP v2, which no retry fixes; the final error
        says so (see _klap_v2_hint).

        Logging policy:
          - Retry attempts log at WARNING so they show in default
            (INFO) logs without enabling debug.
          - A successful retry logs at INFO so the recovery is visible.
          - Final failure logs at ERROR (in addition to raising), so
            the full attempt history is captured even when the caller
            only surfaces the exception text.
        """
        missing_zone = _known_missing_zone(self.address)
        if missing_zone is not None:
            # Logged at ERROR when first found; don't contact the device.
            msg = _missing_zone_message(action, self.address, missing_zone)
            logger.debug(msg)
            raise RuntimeError(msg)

        last_error = None
        total_attempts = 1 + retries
        for attempt in range(total_attempts):
            try:
                # If already inside an async event loop (e.g., MCP server),
                # run in a new thread to avoid "cannot call asyncio.run()
                # from a running event loop" error.
                try:
                    asyncio.get_running_loop()
                    in_loop = True
                except RuntimeError:
                    in_loop = False
                if in_loop:
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, coro_factory())
                        result = future.result(timeout=self.timeout + 10)
                else:
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
                _forget_missing_zone(self.address)
                return result
            except ImportError as e:
                if not is_missing(e, "kasa"):
                    raise BrokenExtraError("kasa", e) from e
                raise MissingExtraError("kasa") from e
            except zoneinfo.ZoneInfoNotFoundError as e:
                # No retry helps: the host lacks the zone python-kasa asked
                # for (see _missing_zone_message). Remembered, so later
                # calls don't contact the device (#76).
                # A KeyError's str() quotes its text; the text reads better.
                detail = str(e.args[0]) if e.args else str(e)
                msg = _missing_zone_message(action, self.address, detail)
                if _record_missing_zone(self.address, detail):
                    logger.error(msg)
                else:
                    logger.debug(msg)  # the periodic recheck, still missing
                raise RuntimeError(msg) from e
            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                if attempt < retries:
                    logger.warning(
                        "Kasa %s attempt %d/%d failed for %s[%d]: "
                        "%s: %s — retrying in 2s",
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
        if error_type == "AuthenticationError" and self._load_credentials():
            msg += _klap_v2_hint()
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
                device = await self._discover_and_update()
                try:
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
