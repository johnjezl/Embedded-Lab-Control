"""
MCP (Model Context Protocol) server for lab controller.

Exposes lab resources, power control, health checks, and SBC management
as MCP resources, tools, and prompts for AI assistant integration.

Usage:
    labctl mcp                    # stdio transport (default)
    labctl mcp --http 8080        # streamable HTTP transport on 127.0.0.1:8080
    labctl mcp --http 8080 --host 0.0.0.0
    labctl mcp --http 8080 --host 0.0.0.0 --cert cert.pem --key key.pem

    # Or directly:
    python -m labctl.mcp_server
"""

import errno
import json
import logging
import os
import shlex
import sys
import threading
import time as _time_mod
from contextlib import contextmanager
from functools import wraps
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from labctl import __version__
from labctl.core import audit

# All logging must go to stderr (stdout is the JSON-RPC channel for stdio transport)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "labctl",
    version=__version__,
    instructions=(
        "Lab Controller MCP server. Provides access to embedded development "
        "lab resources including SBC management, power control, serial ports, "
        "and health monitoring. Use resources to read state, tools to perform actions. "
        "Files for the SD card (images, kernels) can't go in tool arguments: "
        "over HTTP, upload them (list_images says how) and pass the bare "
        "name; over stdio, pass a host path inside mcp.allowed_read_paths "
        "(list_images lists the options)."
    ),
)


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------
#
# Single reviewable classification of every tool. Every hint is explicit:
# the MCP spec defaults destructiveHint and openWorldHint to *true*, so an
# omitted field is not "safe". open_world means the tool touches physical
# hardware (power plugs, SDWire, serial, relays) or the network.
# tests/unit/test_mcp_server.py asserts this table and the registered tools
# match exactly, so a new tool cannot ship without an entry.
#
# name: (title, read_only, destructive, idempotent, open_world)
_TOOL_ANNOTATION_TABLE: dict[str, tuple[str, bool, bool, bool, bool]] = {
    # Power / health
    "power_on": ("Power on SBC", False, False, True, True),
    "power_off": ("Power off SBC", False, True, True, True),
    "power_cycle": ("Power-cycle SBC", False, True, False, True),
    "run_health_check": ("Run health check", False, False, True, True),
    # SBC / port / device inventory (database only)
    "add_sbc": ("Add SBC", False, False, False, False),
    "remove_sbc": ("Remove SBC", False, True, True, False),
    "update_sbc": ("Update SBC", False, False, True, False),
    "assign_serial_port": ("Assign serial port", False, False, True, False),
    "assign_power_plug": ("Assign power plug", False, False, True, False),
    "set_network_address": ("Set network address", False, False, True, False),
    "remove_serial_port": ("Remove serial port", False, True, True, False),
    "remove_network_address": ("Remove network address", False, True, True, False),
    "remove_power_plug": ("Remove power plug", False, True, True, False),
    "add_serial_device": ("Add serial device", False, False, False, False),
    "remove_serial_device": ("Remove serial device", False, True, True, False),
    # SDWire inventory
    "sdwire_add": ("Add SDWire device", False, False, False, False),
    "sdwire_remove": ("Remove SDWire device", False, True, True, False),
    "sdwire_assign": ("Assign SDWire to SBC", False, False, True, False),
    "sdwire_unassign": ("Unassign SDWire", False, True, True, False),
    # Discovery (USB scans)
    "sdwire_discover": ("Discover SDWire devices", True, False, True, True),
    "serial_discover": ("Discover USB-serial adapters", True, False, True, True),
    # SD card mux / contents. sdwire_ls/cat/info are read-only by
    # construction (_sdwire_read_access): a card already on the host is read
    # in place and left there; a card on the DUT is switched to host and back
    # only when the board is known to be OFF, otherwise they refuse.
    "sdwire_to_dut": ("Switch SD card to DUT", False, False, True, True),
    "sdwire_to_host": ("Switch SD card to host", False, True, True, True),
    "sdwire_update": ("Update files on SD card", False, True, False, True),
    "sdwire_ls": ("List SD card directory", True, False, True, True),
    "sdwire_cat": ("Read file from SD card", True, False, True, True),
    "sdwire_info": ("SD card partition info", True, False, True, True),
    "flash_image": ("Flash image to SD card", False, True, False, True),
    # Serial console
    "serial_capture": ("Capture serial output", True, False, True, True),
    "serial_send": ("Send to serial console", False, True, False, True),
    "boot_test": ("Run boot reliability test", False, True, False, True),
    # Claims (database only)
    "claim_sbc": ("Claim SBC", False, False, False, False),
    "release_sbc": ("Release own SBC claim", False, False, True, False),
    "renew_sbc_claim": ("Renew SBC claim", False, False, False, False),
    "list_claims": ("List claims", True, False, True, False),
    "get_claim": ("Get claim", True, False, True, False),
    "request_sbc_release": ("Request claim release", False, False, False, False),
    "force_release_sbc": ("Force-release SBC claim", False, True, True, False),
    # Actuators. actuator_probe only records last_probe_* bookkeeping.
    "actuator_list": ("List actuators", True, False, True, False),
    "actuator_probe": ("Probe actuator", True, False, True, True),
    "actuator_add": ("Add actuator", False, False, False, False),
    "actuator_remove": ("Remove actuator", False, True, True, False),
    "actuator_set": ("Set actuator channel (raw)", False, True, True, True),
    # Bindings. Binding purposes are free-form (power_button, reset, ...), so
    # the verbs are conservatively destructive.
    "bind": ("Bind actuator channel", False, False, False, False),
    "unbind": ("Remove binding", False, True, True, False),
    "bindings_list": ("List bindings", True, False, True, False),
    "actuate": ("Assert latch binding", False, True, True, True),
    "release": ("Release latch binding", False, True, True, True),
    "press": ("Pulse momentary binding", False, True, False, True),
    "actuation_status": ("Binding actuation status", True, False, True, False),
    # Uploaded images (mcp.upload_dir; uploads are PUT /images/<name>)
    "list_images": ("List uploaded images", True, False, True, False),
    "delete_image": ("Delete uploaded image", False, True, True, False),
    # Recovery composites (power transitions)
    "enter_recovery": ("Enter recovery mode", False, True, False, True),
    "exit_recovery": ("Exit recovery mode", False, True, False, True),
}

TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    name: ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )
    for name, (
        title,
        read_only,
        destructive,
        idempotent,
        open_world,
    ) in _TOOL_ANNOTATION_TABLE.items()
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_manager():
    """Get a ResourceManager instance."""
    from labctl.core.config import load_config
    from labctl.core.manager import get_manager

    config = load_config()
    return get_manager(
        config.database_path,
        timeout_seconds=config.database.timeout_seconds,
    )


def _fresh_config():
    """Re-read the startup config file (not the snapshot): for API keys,
    so revocation is immediate and a broken file means no users."""
    from labctl.core.config import load_config

    return load_config(fresh=True)


def _get_config():
    """The config for this call: once run_server has started, the startup
    snapshot (labctl.core.config.snapshot_config)."""
    from labctl.core.config import load_config

    return load_config()


def _resolve_host_path(path: str, access: str) -> tuple[str | None, str | None]:
    """Check a host filesystem path an MCP tool wants to use (D012).

    ``access`` is "read" or "write"; the matching allowlist is
    ``mcp.allowed_read_paths`` / ``mcp.allowed_write_paths``. Returns
    ``(resolved_path, None)`` or ``(None, "Error: ...")``.

    Deny-all when the allowlist is empty. The path must be absolute; it is
    resolved with ``realpath`` (symlinks and ``..``) *before* the prefix
    check, so neither can escape an allowed directory. Callers should use
    the returned resolved path from then on.
    """
    mcp_cfg = _get_config().mcp
    roots = (
        mcp_cfg.allowed_read_paths if access == "read" else mcp_cfg.allowed_write_paths
    )
    key = f"mcp.allowed_{access}_paths"
    if not roots:
        return None, (
            f"Error: host file {access} access over MCP is disabled. "
            f"Add the directory to {key} in config.yaml."
        )
    if "\0" in path:
        # realpath() raises ValueError on an embedded NUL; refuse cleanly.
        return None, "Error: host path contains a NUL byte"
    if not os.path.isabs(path):
        return None, f"Error: host path must be absolute: {path!r}"
    resolved = os.path.realpath(path)
    for root in roots:
        root_resolved = os.path.realpath(root)
        if os.path.commonpath([resolved, root_resolved]) == root_resolved:
            return resolved, None
    # Don't echo `resolved`: realpath follows symlinks anywhere on the host,
    # so repeating it would let a client map symlink targets (e.g. under
    # /proc or home directories) that the allowlist is meant to hide.
    return None, (
        f"Error: {path!r} is outside {key} (symlinks and '..' are "
        f"resolved before checking): {', '.join(roots)}"
    )


def _host_open_error(path: str, e: OSError) -> str:
    """User-facing message for a failed safe open of a checked host path."""
    if e.errno == errno.EINVAL:
        return f"Error: not a regular file: {path!r}"
    if e.errno == errno.ENOENT:
        return f"Error: file not found: {path!r}"
    if e.errno in (errno.EACCES, errno.EPERM):
        return f"Error: permission denied: {path!r}"
    # ELOOP/ENOTDIR: a symlink appeared on the path after the check.
    return (
        f"Error: could not open {path!r} safely (it may have changed "
        f"since it was checked): {e.strerror or e}"
    )


def _image_name_to_path(path: str) -> tuple[str | None, str | None]:
    """A bare name (no "/") is an image in mcp.upload_dir (D014): return
    its path there. Other paths are returned unchanged."""
    from labctl.core.images import name_error

    if "/" in path:
        return path, None
    upload_dir = _get_config().mcp.upload_dir
    if not upload_dir:
        return None, (
            f"Error: {path!r} is not an absolute path, and bare image names "
            "need mcp.upload_dir to be set"
        )
    err = name_error(path)
    if err:
        return None, f"Error: {err}"
    return os.path.join(upload_dir, path), None


def _in_upload_dir(resolved: str) -> bool:
    """Whether resolved host path ``resolved`` is in mcp.upload_dir."""
    upload_dir = _get_config().mcp.upload_dir
    if not upload_dir:
        return False
    root = os.path.realpath(upload_dir)
    return os.path.commonpath([resolved, root]) == root


def _open_host_read(path: str) -> tuple[int | None, str | None]:
    """Check ``path`` against mcp.allowed_read_paths and open it, no symlinks.

    A bare name (no "/") means that image in mcp.upload_dir.

    Returns ``(fd, None)`` or ``(None, "Error: ...")``. The file is opened
    at check time (``safe_open``: every component O_NOFOLLOW on the
    resolved path) and callers read from the descriptor, so a symlink
    swapped in afterwards can't redirect the read. Caller closes the fd.
    """
    from labctl.core.safe_open import open_file_nofollow

    path, err = _image_name_to_path(path)
    if err:
        return None, err
    resolved, err = _resolve_host_path(path, "read")
    if err:
        return None, err
    if os.path.basename(resolved).startswith(".") and (
        _in_upload_dir(resolved) or _is_upload_dir_path(os.path.dirname(resolved))
    ):
        # Uploads in progress (.upload-*): unfinished and unverified. Also
        # under another name for upload_dir (bind mount, case alias).
        return None, f"Error: {path!r} is not an image (dot files in mcp.upload_dir)"
    try:
        # Refuses non-regular files (e.g. a planted FIFO) without blocking.
        fd = open_file_nofollow(resolved, os.O_RDONLY)
    except OSError as e:
        return None, _host_open_error(path, e)
    return fd, None


def _open_host_write_dir(path: str) -> tuple[int | None, str | None, str | None]:
    """Check ``path`` against mcp.allowed_write_paths; open/create it safely.

    Returns ``(dir_fd, resolved, None)`` or ``(None, None, "Error: ...")``.
    Files must then be created relative to ``dir_fd``. Caller closes it.
    """
    from labctl.core.safe_open import open_dir_nofollow

    resolved, err = _resolve_host_path(path, "write")
    if err:
        return None, None, err
    refused = (
        None,
        None,
        f"Error: {path!r} is inside mcp.upload_dir; tools don't write there",
    )
    if _in_upload_dir(resolved):
        # Run files would overwrite (truncate) uploaded images in place.
        return refused
    try:
        fd = open_dir_nofollow(resolved, create=True)
    except OSError as e:
        if e.errno == errno.ENOTDIR:
            # Missing directories are created, so this is a file in the way
            # (the path was resolved, so a symlink here appeared since).
            return (
                None,
                None,
                (
                    f"Error: not a directory: {path!r} (a file is in the way, "
                    "or the path changed since it was checked)"
                ),
            )
        return None, None, _host_open_error(path, e)
    # The same directory under another name (a bind mount, letter case on
    # a case-insensitive file system) passes the path check above.
    if _is_upload_dir_stat(os.fstat(fd)):
        os.close(fd)
        return refused
    return fd, resolved, None


def _is_upload_dir_stat(st: os.stat_result) -> bool:
    """Whether the directory ``st`` describes is mcp.upload_dir itself,
    whatever name reached it."""
    upload_dir = _get_config().mcp.upload_dir
    if not upload_dir:
        return False
    try:
        upload = os.stat(upload_dir)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (upload.st_dev, upload.st_ino)


def _is_upload_dir_path(path: str) -> bool:
    try:
        return _is_upload_dir_stat(os.stat(path))
    except OSError:
        return False


def _open_copy_sources(
    specs: list[str],
) -> tuple[list[tuple[int, str]], list[str], str | None]:
    """Check and open the host *source* side of "source:dest" copy specs.

    Returns ``(pairs, names, error)``: ``pairs`` are (open fd, dest), read
    from directly so the checked file is what's copied; ``names`` are the
    file names the caller gave, so a symlinked source copied into a
    directory keeps the caller's name (``update_files(source_names=...)``).
    Specs without ":" are skipped (callers report them); ``dest`` is a path
    on the SD card, not on the host, so it isn't checked. On error, any
    descriptors already opened are closed. Caller closes the rest
    (``_close_fds``).
    """
    pairs, names = [], []
    for spec in specs:
        if ":" not in spec:
            continue
        src, dest = spec.split(":", 1)
        fd, err = _open_host_read(src)
        if err:
            _close_fds(pairs)
            return [], [], err
        pairs.append((fd, dest))
        names.append(os.path.basename(src))
    return pairs, names, None


def _close_fds(pairs: list[tuple[int, str]]) -> None:
    for fd, _ in pairs:
        try:
            os.close(fd)
        except OSError:
            pass


def _sbc_to_dict(sbc) -> dict:
    """Convert an SBC model to a JSON-serializable dict."""
    return sbc.to_dict(include_ids=False)


# --- Session identity ---
#
# stdio: one process = one session. Over authenticated HTTP each user is
# its own claimant (`<process id>:<username>`, and the claim records the
# username as its owner, so the user's other sessions and a restarted
# server count as the same claimant); unauthenticated HTTP clients share
# the process identity. The claim sweep still sees the process id at the
# front. run_server() switches the prefix and kind to mcp-http.

_SERVER_START_EPOCH = int(_time_mod.time())
_SESSION_ID = f"mcp-stdio:{os.getpid()}-{_SERVER_START_EPOCH}"
_SESSION_KIND = "mcp-stdio"
_AGENT_NAME: str | None = None  # stdio / unauthenticated HTTP
_USER_AGENT_NAMES: dict[str, str] = {}  # authenticated HTTP, per user


def _set_transport(transport: str) -> None:
    global _SESSION_ID, _SESSION_KIND
    _SESSION_KIND = "mcp-http" if transport == "http" else "mcp-stdio"
    _SESSION_ID = f"{_SESSION_KIND}:{os.getpid()}-{_SERVER_START_EPOCH}"


def _get_session_id() -> str:
    user = _authenticated_user()
    return f"{_SESSION_ID}:{user}" if user else _SESSION_ID


def _get_agent_name() -> str:
    user = _authenticated_user()
    if user:
        return _USER_AGENT_NAMES.get(user) or user
    return _AGENT_NAME or f"unnamed-{_SESSION_ID[:16]}"


def _set_agent_name(name: str) -> None:
    global _AGENT_NAME
    user = _authenticated_user()
    if user:
        _USER_AGENT_NAMES[user] = name
    else:
        _AGENT_NAME = name


def _is_process_session(session_id: str) -> bool:
    """True for this process's session IDs (any user)."""
    return session_id == _SESSION_ID or session_id.startswith(_SESSION_ID + ":")


# --- Claim enforcement helper ---


def _caller_is_operator() -> bool:
    """Operators may force-release other users' claims (D013). Over
    authenticated HTTP that is the user's ``role`` (carried in the access
    token's scopes by _ApiKeyVerifier); without auth there is one shared
    identity, which is an operator."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    from labctl.core.config import ROLE_OPERATOR

    token = get_access_token()
    return token is None or ROLE_OPERATOR in (token.scopes or [])


def _caller_actor():
    from labctl.core.models import ClaimActor

    return ClaimActor(
        session_id=_get_session_id(),
        owner=_authenticated_user(),
        operator=_caller_is_operator(),
    )


def _claimed_error(sbc_name: str, claim) -> str:
    """``sbc_claimed`` for ``sbc_name``; ``claim`` may be on another board
    that shares its hardware (plug, SDWire, console, relay)."""
    holder_board = claim.sbc_name or sbc_name
    if claim.is_active:
        hints = [
            "Wait for claim to expire or be released",
            "Request early release with request_sbc_release",
        ]
        if _caller_is_operator():
            hints.append("Operator override with force_release_sbc")
    else:
        # Past its deadline: release requests and force-release only act
        # on active claims, so the only way is to wait out the grace.
        hints = ["Retry claim_sbc once the grace period ends"]
    # claim.to_dict() carries time_remaining_seconds for the caller.
    shared = (
        f" (it shares hardware with '{sbc_name}')" if holder_board != sbc_name else ""
    )
    if claim.is_active:
        until = f"until {claim.expires_at.isoformat()}"
    else:
        # Past its deadline but inside the grace period.
        until = (
            f"(expired at {claim.expires_at.isoformat()}; it can be claimed "
            f"once its {_get_config().claims.grace_period_seconds}s grace "
            "period ends)"
        )
    return json.dumps(
        {
            "error": "sbc_claimed",
            "sbc_name": holder_board,
            "message": (
                f"SBC '{holder_board}' is claimed by "
                f"'{claim.agent_name}' {until}{shared}"
            ),
            "claim": claim.to_dict(),
            "hints": hints,
        }
    )


def _check_claim(
    manager,
    sbc_name: str,
    mutating: bool = True,
    *,
    direct: bool = True,
    shared: bool = True,
    for_sbc: str | None = None,
) -> str | None:
    """Check claim state for an SBC-targeting tool call.

    - The caller's own claim (this session, or the same authenticated
      user from any session) → heartbeat + return None (proceed).
    - Someone else's claim + mutating → structured JSON error string,
      also when it is on a board sharing hardware with this one.
    - No claim → proceed, unless ``claims.require_claim`` is on and the
      call is mutating (structured ``claim_required`` error).
    - Read-only calls never refuse.

    ``direct=False`` is for boards a call affects only through shared
    hardware: only someone else's claim on that board refuses.
    ``for_sbc`` names the board the call is about, when that isn't
    ``sbc_name`` (a board sharing its hardware), for the error message.
    ``shared=False`` is for calls that only change this board's own
    records (configuration), which don't touch hardware it shares with
    another board, so that board's claim doesn't matter.
    """
    config = _get_config()
    if not config.claims.enabled:
        return None

    from labctl.core.models import (
        ClaimConflict,
        ClaimRequiredError,
        UnknownSBCError,
    )

    actor = _caller_actor()
    try:
        if not mutating:
            claim = manager.get_active_claim(sbc_name)
            if claim is not None and actor.holds(claim):
                manager.touch_claim(claim)
            return None
        manager.check_claim(
            sbc_name,
            actor,
            require_claim=direct and config.claims.require_claim,
            include_shared=direct and shared,
            heartbeat=direct,
        )
    except UnknownSBCError:
        return None
    except ClaimConflict as exc:
        return _claimed_error(for_sbc or sbc_name, exc.claim)
    except ClaimRequiredError as exc:
        return json.dumps(
            {
                "error": "claim_required",
                "sbc_name": sbc_name,
                "message": str(exc),
                "hints": [f"Claim it first with claim_sbc('{sbc_name}', ...)"],
            }
        )
    return None


def _check_claims_of(manager, sbc_names, for_sbc: str | None = None) -> str | None:
    """Refuse if someone else claims one of these SBCs (boards a call on
    ``for_sbc`` reaches through shared hardware; require_claim doesn't
    apply)."""
    for name in sorted(n for n in sbc_names if n):
        claim_err = _check_claim(manager, name, direct=False, for_sbc=for_sbc)
        if claim_err:
            return claim_err
    return None


def _check_shared_hardware(manager, sbc_name: str, **hardware) -> str | None:
    """Before wiring hardware to ``sbc_name``, claim-check the boards that
    already have it: otherwise an agent could attach a claimed board's
    plug, SDWire or console to an unclaimed board and drive it from there.
    ``hardware`` is passed to ResourceManager.sbcs_sharing_hardware."""
    if not _get_config().claims.enabled:
        return None
    others = set(manager.sbcs_sharing_hardware(**hardware)) - {sbc_name}
    return _check_claims_of(manager, others, for_sbc=sbc_name)


def _claim_advisory(manager, sbc_name: str) -> str:
    """Return advisory text about pending release requests, or empty string.

    Called after a tool completes successfully. If the caller holds the
    claim and there are pending release requests, returns a human-readable
    notice the AI agent can act on.
    """
    from labctl.core.models import UnknownSBCError

    try:
        claim = manager.get_active_claim(sbc_name)
    except UnknownSBCError:
        return ""
    if claim is None or not _caller_actor().holds(claim):
        return ""
    if not claim.pending_requests:
        return ""
    lines = ["[claim advisory] Pending release request(s):"]
    for req in claim.pending_requests:
        lines.append(f"  - {req.requested_by}: {req.reason}")
    return "\n".join(lines)


# mcp 2.x runs sync handlers (tools *and* resources) on worker threads; 1.x
# ran them inline on the event loop, which serialized them. Tools and some
# resources drive shared physical hardware — power plugs, SD muxes, serial
# consoles — so keep the 1.x one-at-a-time semantics for everything that
# touches hardware rather than let e.g. power_cycle and flash_image, or a
# power-state read and power_cycle, interleave on the same device.
# Not reentrant: a locked handler must not call another locked handler.
_HARDWARE_LOCK = threading.Lock()


def _serialized(func):
    """Run a handler under the process-wide hardware lock (see above).

    Use directly on resources that query hardware; tools get it via
    ``_with_mcp_activity``. DB-only resources and prompts stay unlocked.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        with _HARDWARE_LOCK:
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Two-step confirmation for destructive tools (D012)
# ---------------------------------------------------------------------------
#
# Annotations are hints a client may ignore, so the server enforces them:
# a tool annotated destructive, called without a token, does nothing and
# returns a plan plus a confirm_token. The token is random, single-use,
# expires after _CONFIRM_TTL_SECONDS, and is bound to the tool name and a
# digest of the exact (defaulted) arguments, so it can't be replayed or
# reused for a different call. Any attempt consumes it.

_CONFIRM_TTL_SECONDS = 120
_DESTRUCTIVE_TOOLS = frozenset(
    name for name, ann in TOOL_ANNOTATIONS.items() if ann.destructive_hint
)
# Tool -> function(arguments) -> dict shown as "will_do" in its
# confirmation (e.g. serial_send's exact bytes); ValueError refuses the call.
# The token is bound to the plan too: if it differs when the token is
# redeemed (e.g. the board's stored cycle delay changed), the call is
# refused and the caller asked for a new plan.
_CONFIRM_PREVIEWS: dict = {}
# token -> (tool, call digest, expiry, user it was issued to (None = no
# auth), plan digest)
_pending_confirmations: dict[str, tuple[str, str, float, str | None, str]] = {}
_confirm_lock = threading.Lock()


def _confirmation_required(tool_name: str) -> bool:
    """Whether ``tool_name`` must be confirmed (config: mcp.confirm_*)."""
    if tool_name not in _DESTRUCTIVE_TOOLS:
        return False
    mcp_cfg = _get_config().mcp
    return mcp_cfg.confirm_destructive and tool_name not in mcp_cfg.confirm_exempt


def _call_digest(tool_name: str, arguments: dict) -> str:
    import hashlib

    canonical = json.dumps(
        {"tool": tool_name, "args": arguments}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _issue_confirmation(
    tool_name: str, arguments: dict, preview: dict | None = None
) -> str:
    import secrets

    token = secrets.token_urlsafe(16)
    now = _time_mod.monotonic()
    with _confirm_lock:
        # Drop expired tokens so the table can't grow without bound.
        for stale in [
            t for t, (_, _, exp, _, _) in _pending_confirmations.items() if exp < now
        ]:
            del _pending_confirmations[stale]
        _pending_confirmations[token] = (
            tool_name,
            _call_digest(tool_name, arguments),
            now + _CONFIRM_TTL_SECONDS,
            _authenticated_user(),
            _plan_digest(preview),
        )
    title = TOOL_ANNOTATIONS[tool_name].title
    response = {
        "status": "confirmation_required",
        "tool": tool_name,
        "action": title,
        "arguments": arguments,
    }
    if preview:
        response["will_do"] = preview
    return json.dumps(
        {
            **response,
            "confirm_token": token,
            "expires_in_seconds": _CONFIRM_TTL_SECONDS,
            "message": (
                f"'{title}' is destructive and has NOT been performed. Review "
                f"the arguments, then call {tool_name} again with exactly the "
                "same arguments plus confirm_token to proceed."
            ),
        },
        indent=2,
        default=str,
    )


def _plan_digest(preview) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(preview, sort_keys=True, default=str).encode()
    ).hexdigest()


def _current_plan(tool_name: str, arguments: dict):
    """The tool's "will_do" plan for these arguments now (None without a
    preview); a call the preview refuses is a plan of its own."""
    preview = _CONFIRM_PREVIEWS.get(tool_name)
    if preview is None:
        return None
    try:
        return preview(arguments)
    except ValueError as e:
        return {"refused": str(e)}


def _redeem_confirmation(
    tool_name: str, arguments: dict, token: str, plan=None
) -> str | None:
    """Consume ``token``; return None if valid for this call, else an error.

    ``plan`` is the call's "will_do" plan now; it must match the plan the
    token was issued with.

    Over authenticated HTTP a token is only good for the user it was issued
    to: tokens are random, but they appear in tool output and transcripts.
    """
    user = _authenticated_user()
    with _confirm_lock:
        entry = _pending_confirmations.get(token)
        # Another user's attempt must not burn the owner's token.
        if entry is not None and entry[3] == user:
            del _pending_confirmations[token]
    if entry is None:
        return "unknown or already-used confirm_token"
    issued_for, digest, expires, issued_to, plan_digest = entry
    if issued_to != user:
        return "confirm_token was issued to a different user"
    if _time_mod.monotonic() > expires:
        return "confirm_token expired"
    if issued_for != tool_name or digest != _call_digest(tool_name, arguments):
        return (
            "confirm_token was issued for a different call (tool or arguments differ)"
        )
    if plan_digest != _plan_digest(plan):
        return (
            "the plan changed since confirm_token was issued (now: "
            f"{json.dumps(plan, sort_keys=True, default=str)}), e.g. the "
            "board's stored cycle delay was changed"
        )
    return None


def _with_confirmation(func):
    """Add the confirm_token step to a destructive tool (see above).

    The wrapper exposes an extra optional ``confirm_token`` parameter in
    the tool's signature (so it appears in the MCP input schema) and adds a
    note to the description. Non-destructive tools are returned unchanged.
    """
    import inspect

    name = func.__name__
    if name not in _DESTRUCTIVE_TOOLS:
        return func
    sig = inspect.signature(func)

    @wraps(func)
    def wrapper(*args, confirm_token: Optional[str] = None, **kwargs):
        if not _confirmation_required(name):
            return func(*args, **kwargs)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        if confirm_token is None:
            preview = _CONFIRM_PREVIEWS.get(name)
            try:
                extra = preview(arguments) if preview else None
            except ValueError as e:
                # Refuse a call that would fail anyway before handing out
                # a token for it.
                return f"Error: {e}"
            return _issue_confirmation(name, arguments, extra)
        problem = _redeem_confirmation(
            name, arguments, confirm_token, _current_plan(name, arguments)
        )
        if problem:
            return json.dumps(
                {
                    "status": "confirmation_failed",
                    "tool": name,
                    "error": problem,
                    "message": "Nothing was done. Call again without "
                    "confirm_token to get a new plan and token.",
                }
            )
        return func(*args, **kwargs)

    params = list(sig.parameters.values())
    params.append(
        inspect.Parameter(
            "confirm_token",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Optional[str],
        )
    )
    wrapper.__signature__ = sig.replace(parameters=params)
    # The description is fixed at import, but confirmation can be turned off
    # or exempted per tool in config, so it must not promise a harmless
    # first call: say what the default is and how to tell what happened.
    wrapper.__doc__ = (func.__doc__ or "").rstrip() + (
        "\n\n    DESTRUCTIVE. By default this requires confirmation: a call "
        "without confirm_token returns status=confirmation_required with a "
        "plan and confirm_token and does nothing; call again with the same "
        "arguments plus confirm_token to act. The server can be configured "
        "to skip confirmation (mcp.confirm_destructive / mcp.confirm_exempt): "
        "if a response is not status=confirmation_required, the action WAS "
        "performed. Never call this just to preview.\n"
    )
    return wrapper


def _with_mcp_activity(func):
    """Serialize one tool call and scope audit attribution to the MCP session.

    Destructive tools also get the two-step confirmation (_with_confirmation)
    as the *outer* layer: a plan is issued, and a confirm_token checked and
    consumed, when the call arrives, before waiting for the hardware lock.
    Otherwise a confirming call queued behind a long boot_test/flash could
    be refused as expired although it arrived in time. Issuing a plan
    touches no hardware, so it needs no lock.
    """
    from labctl.core import audit

    @wraps(func)
    def locked(*args, **kwargs):
        # The session ID names the user over authenticated HTTP.
        with _HARDWARE_LOCK, audit.activity_context(_get_session_id(), "mcp"):
            return func(*args, **kwargs)

    return _with_confirmation(locked)


def _sdwire_read_error(error: str, message: str, **extra) -> str:
    payload = {"error": error, "message": message}
    payload.update(extra)
    return json.dumps(payload)


def _structured_claim_advisory(manager, sbc_name: str) -> list[dict]:
    """Return pending release requests in structured form for JSON tools."""
    from labctl.core.models import UnknownSBCError

    try:
        claim = manager.get_active_claim(sbc_name)
    except UnknownSBCError:
        return []
    if claim is None or not _caller_actor().holds(claim):
        return []
    return [
        {"requested_by": req.requested_by, "reason": req.reason}
        for req in claim.pending_requests
    ]


def _sdwire_host_switch_guard_mcp(
    sbc_name: str, sbc, force: bool = False, allow_force_override: bool = False
) -> str | None:
    """Refuse host-mode switching unless the SBC's plug reports it OFF
    (power_guard.check_board_off: no plug, a plug that can't be read, or
    another state refuse too), unless ``force``."""
    if force:
        return None
    from labctl.sdwire.power_guard import (
        POWERED_ON,
        board_off_problem_text,
        check_board_off,
    )

    check = check_board_off(sbc)
    if check.is_off:
        return None
    advice = (
        "Power off before switching SD to host mode."
        if check.problem == POWERED_ON
        else "Make sure the board is off first."
    )
    override = (
        " Use force=true to override (risks SD card corruption)."
        if allow_force_override
        else ""
    )
    return f"Error: {board_off_problem_text(sbc_name, check)}. {advice}{override}"


def _sdwire_read_power_refusal(sbc_name: str, sbc) -> str | None:
    """Strict power check for the read-only SD tools: proceed only on OFF
    (power_guard.check_board_off, as ``sdwire_to_host``), because pulling
    the card from a running board can corrupt it. Returns a
    ``_sdwire_read_error`` payload or None.
    """
    from labctl.sdwire.power_guard import (
        POWERED_ON,
        board_off_problem_text,
        check_board_off,
    )

    check = check_board_off(sbc)
    if check.is_off:
        return None
    text = board_off_problem_text(sbc_name, check)
    text = text[0].upper() + text[1:]
    if check.problem == POWERED_ON:
        return _sdwire_read_error(
            "powered_on", f"{text}. Power off before switching SD to host mode."
        )
    return _sdwire_read_error(
        "power_unknown",
        f"{text}. Power the board off, or switch the card explicitly with "
        "sdwire_to_host, then retry.",
    )


# Second look before treating the card as on the DUT (see below).
_HOST_RECHECK_SECONDS = 1.0


def _sdwire_read_access(sbc_name: str, sbc, ctrl) -> tuple[bool, str | None]:
    """Decide how a read-only SD tool may reach the card without side effects.

    Returns ``(needs_switch, refusal)``:
    - Card already on the host (``ctrl.card_on_host()``, which re-checks the
      reader for media rather than trusting a possibly stale size): read it
      in place and leave the mux alone; the DUT is not using the card, so
      its power state doesn't matter.
    - Card on the DUT: switch to host and back only if the board is known to
      be OFF (see ``_sdwire_read_power_refusal``); otherwise refuse.
    Callers hold the hardware lock, so detection and switching aren't raced
    by other calls in this MCP server. Other processes (the CLI, another
    MCP server) are not covered; claims are the cross-process guard.
    """
    if ctrl.card_on_host():
        return False, None
    # A card switched to the host moments ago by another process (CLI,
    # another session) may still be initialising and probe as "no medium".
    # Concluding "on DUT" would make the cleanup switch it there, so check
    # once more before deciding. (The mux can't be read back directly.)
    _time_mod.sleep(_HOST_RECHECK_SECONDS)
    if ctrl.card_on_host():
        return False, None
    refusal = _sdwire_read_power_refusal(sbc_name, sbc)
    if refusal:
        return False, refusal
    return True, None


# ---------------------------------------------------------------------------
# Resources (read-only data)
# ---------------------------------------------------------------------------


@mcp.resource("lab://sbcs")
def list_sbcs() -> str:
    """List all SBCs in the lab with their status, project, IP, and power plug info."""
    manager = _get_manager()
    sbcs = manager.list_sbcs()
    return json.dumps([_sbc_to_dict(s) for s in sbcs], indent=2)


@mcp.resource("lab://sbcs/{sbc_name}")
def get_sbc_details(sbc_name: str) -> str:
    """Get full details for a specific SBC including serial ports, network, and power."""  # noqa: E501
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})
    return json.dumps(_sbc_to_dict(sbc), indent=2)


@mcp.resource("lab://power/{sbc_name}")
@_serialized
def get_power_state(sbc_name: str) -> str:
    """Get current power state for an SBC (on/off/unknown)."""
    from labctl.power import PowerController

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})
    if not sbc.power_plug:
        return json.dumps({"error": f"No power plug assigned to '{sbc_name}'"})

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        state = controller.get_state()
        return json.dumps(
            {
                "sbc": sbc_name,
                "state": state.value,
                "plug_type": sbc.power_plug.plug_type.value,
                "address": sbc.power_plug.address,
            }
        )
    except RuntimeError as e:
        return json.dumps({"sbc": sbc_name, "state": "error", "error": str(e)})


@mcp.resource("lab://serial-devices")
def list_serial_devices() -> str:
    """List all registered USB-serial adapters."""
    manager = _get_manager()
    devices = manager.list_serial_devices()
    return json.dumps(
        [
            {
                "name": d.name,
                "usb_path": d.usb_path,
                "vendor": d.vendor,
                "model": d.model,
                "serial_number": d.serial_number,
            }
            for d in devices
        ],
        indent=2,
    )


@mcp.resource("lab://ports")
def list_ports() -> str:
    """List all serial port assignments with aliases and SBC mappings."""
    manager = _get_manager()
    ports = manager.list_serial_ports()
    sbc_names = {s.id: s.name for s in manager.list_sbcs()}
    return json.dumps(
        [
            {
                "sbc": sbc_names.get(p.sbc_id, f"#{p.sbc_id}"),
                "type": p.port_type.value,
                "alias": p.alias,
                "device": p.device_path,
                "tcp_port": p.tcp_port,
                "baud_rate": p.baud_rate,
                "serial_device": p.serial_device.name if p.serial_device else None,
            }
            for p in ports
        ],
        indent=2,
    )


@mcp.resource("lab://health/{sbc_name}")
@_serialized
def get_health(sbc_name: str) -> str:
    """Run a live health check on an SBC and return results."""
    from labctl.core.config import load_config
    from labctl.health.checks import HealthChecker

    manager = _get_manager()
    config = load_config()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return json.dumps({"error": f"SBC '{sbc_name}' not found"})

    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )
    summary = checker.check_sbc(sbc)

    return json.dumps(
        {
            "sbc": sbc_name,
            "ping": (
                {
                    "success": summary.ping_result.success,
                    "message": summary.ping_result.message,
                }
                if summary.ping_result
                else None
            ),
            "serial": (
                {
                    "success": summary.serial_result.success,
                    "message": summary.serial_result.message,
                }
                if summary.serial_result
                else None
            ),
            "power": {
                "state": summary.power_state.value if summary.power_state else None,
                "success": (
                    summary.power_result.success if summary.power_result else None
                ),
                "message": (
                    summary.power_result.message if summary.power_result else None
                ),
            },
            "recommended_status": (
                summary.recommended_status.value if summary.recommended_status else None
            ),
        },
        indent=2,
    )


@mcp.resource("lab://status")
@_serialized
def get_status_overview() -> str:
    """Get a dashboard-style overview of all SBCs with power states."""
    from labctl.power import PowerController

    manager = _get_manager()
    sbcs = manager.list_sbcs()
    result = []

    for sbc in sbcs:
        entry = {
            "name": sbc.name,
            "project": sbc.project or "-",
            "status": sbc.status.value,
            "ip": sbc.primary_ip or "-",
            "power": "-",
        }

        if sbc.power_plug:
            try:
                controller = PowerController.from_plug(sbc.power_plug)
                state = controller.get_state()
                entry["power"] = state.value
            except Exception:
                entry["power"] = "error"

        result.append(entry)

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tools (actions)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["power_on"])
@_with_mcp_activity
def power_on(sbc_name: str) -> str:
    """Turn on power to an SBC.

    Relays bound to the SBC with sample_phase=pre_power (e.g. a recovery
    strap) are set to their desired state first; if one can't be set,
    power is not switched.

    Args:
        sbc_name: Name of the SBC to power on
    """
    from labctl.power import PowerController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    from labctl.actuators.runtime import ActuationError, power_on_with_bindings

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if power_on_with_bindings(manager, sbc, controller):
            result = f"Power ON: {sbc_name}"
            advisory = _claim_advisory(manager, sbc_name)
            return f"{result}\n\n{advisory}" if advisory else result
        return f"Failed to power on {sbc_name}"
    except (RuntimeError, ActuationError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["power_off"])
@_with_mcp_activity
def power_off(sbc_name: str) -> str:
    """Turn off power to an SBC.

    Args:
        sbc_name: Name of the SBC to power off
    """
    from labctl.power import PowerController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if controller.power_off():
            result = f"Power OFF: {sbc_name}"
            advisory = _claim_advisory(manager, sbc_name)
            return f"{result}\n\n{advisory}" if advisory else result
        return f"Failed to power off {sbc_name}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["power_cycle"])
@_with_mcp_activity
def power_cycle(sbc_name: str, delay: Optional[float] = None) -> str:
    """Power cycle an SBC (turn off, wait, turn on).

    Relays bound with sample_phase=pre_power are set to their desired
    state before power comes back on. They are checked before power-off,
    so a relay that can't be opened leaves the board running.

    The plug outlet is switched, so boards sharing the SBC's outlet are
    cycled too: their pre_power relays are checked and set the same way,
    and a claim someone else holds on one of them refuses the call.

    Args:
        sbc_name: Name of the SBC to power cycle
        delay: Seconds to wait between off and on, 0 to 86400. Omitted:
            the board's stored cycle delay, or 3.0 if none is set. That
            is also the minimum: a smaller value is raised to it (a
            negative one is an error).
    """
    from labctl.power import PowerController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: No power plug assigned to '{sbc_name}'"

    from labctl.actuators.runtime import (
        ActuationError,
        board_cycle_delay,
        power_cycle_with_bindings,
    )

    try:
        effective = board_cycle_delay(sbc, delay)
    except ValueError as e:  # nothing switched
        return f"Error: {e}"
    try:
        controller = PowerController.from_plug(sbc.power_plug)
        if power_cycle_with_bindings(manager, sbc, controller, effective):
            result = f"Power cycled: {sbc_name} (delay: {effective}s)"
            if delay is not None and effective > delay:
                result += f"; {delay}s was raised to the board's minimum"
            advisory = _claim_advisory(manager, sbc_name)
            return f"{result}\n\n{advisory}" if advisory else result
        return f"Failed to power cycle {sbc_name}"
    except (RuntimeError, ActuationError, ValueError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["run_health_check"])
@_with_mcp_activity
def run_health_check(sbc_name: Optional[str] = None) -> str:
    """Run health checks on one or all SBCs and return results.

    Args:
        sbc_name: Name of a specific SBC to check, or omit for all SBCs
    """
    from labctl.core.config import load_config
    from labctl.health.checks import HealthChecker

    manager = _get_manager()
    config = load_config()

    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout,
        serial_timeout=config.health.serial_timeout,
    )

    if sbc_name:
        sbc = manager.get_sbc_by_name(sbc_name)
        if not sbc:
            return f"Error: SBC '{sbc_name}' not found"
        sbcs = [sbc]
    else:
        sbcs = manager.list_sbcs()

    results = checker.check_all(sbcs)

    output = []
    for name, summary in results.items():
        entry = {"sbc": name}
        if summary.ping_result:
            entry["ping"] = summary.ping_result.success
        if summary.serial_result:
            entry["serial"] = summary.serial_result.success
        if summary.power_state:
            entry["power"] = summary.power_state.value
        if summary.recommended_status:
            entry["status"] = summary.recommended_status.value
        output.append(entry)

    return json.dumps(output, indent=2)


@mcp.tool(annotations=TOOL_ANNOTATIONS["add_sbc"])
@_with_mcp_activity
def add_sbc(
    name: str,
    project: Optional[str] = None,
    description: Optional[str] = None,
    ssh_user: str = "root",
) -> str:
    """Create a new SBC record in the lab inventory.

    Args:
        name: Unique name for the SBC (e.g., 'rpi4-01', 'jetson-nano-2')
        project: Project this SBC belongs to
        description: Human-readable description
        ssh_user: SSH username for remote access (default: root)
    """
    manager = _get_manager()

    try:
        sbc = manager.create_sbc(
            name=name,
            project=project,
            description=description,
            ssh_user=ssh_user,
        )
        return json.dumps({"created": _sbc_to_dict(sbc)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["remove_sbc"])
@_with_mcp_activity
def remove_sbc(name: str) -> str:
    """Remove an SBC and all its assignments from the lab inventory.

    Args:
        name: Name of the SBC to remove
    """
    manager = _get_manager()
    claim_err = _check_claim(manager, name, shared=False)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        return f"Error: SBC '{name}' not found"

    from labctl.core.models import ClaimConflict

    # The check above passed, so any claim is the caller's own: release it
    # (so the release is recorded; an expired one is just deleted) before
    # removing the board.
    claim = manager.get_active_claim(name)
    released = False
    if claim is not None and _caller_actor().holds(claim):
        from labctl.core.models import ClaimNotFoundError, NotClaimantError

        try:
            manager.release_claim(
                name,
                _get_session_id(),
                owner=_authenticated_user(),
                released_by=_get_agent_name(),
            )
            released = True
        except ClaimNotFoundError:
            pass  # expired or released meanwhile: nothing to release
        except NotClaimantError:  # lapsed, and someone else claimed it
            current = manager.get_active_claim(name)
            if current is not None:
                return _claimed_error(name, current)
    try:
        deleted = manager.delete_sbc(sbc.id)
    except ClaimConflict as exc:  # claimed by someone else since the check
        err = json.loads(_claimed_error(name, exc.claim))
        if released:
            err["message"] += "; your own claim on it was released first"
        return json.dumps(err)
    if deleted:
        result = f"Removed SBC: {name}"
        advisory = _claim_advisory(manager, name)
        return f"{result}\n\n{advisory}" if advisory else result
    return f"Failed to remove SBC: {name}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["update_sbc"])
@_with_mcp_activity
def update_sbc(
    name: str,
    rename: Optional[str] = None,
    project: Optional[str] = None,
    description: Optional[str] = None,
    ssh_user: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Update an SBC's properties.

    Args:
        name: Current name of the SBC
        rename: New name for the SBC (optional)
        project: New project name (optional)
        description: New description (optional)
        ssh_user: New SSH username (optional)
        status: New status: unknown, online, offline, booting, error (optional)
    """
    from labctl.core.models import Status

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(name)
    if not sbc:
        return f"Error: SBC '{name}' not found"
    claim_err = _check_claim(manager, name, shared=False)
    if claim_err:
        return claim_err

    status_enum = Status(status) if status else None

    try:
        updated = manager.update_sbc(
            sbc.id,
            name=rename,
            project=project,
            description=description,
            ssh_user=ssh_user,
            status=status_enum,
        )
        return json.dumps({"updated": _sbc_to_dict(updated)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["assign_serial_port"])
@_with_mcp_activity
def assign_serial_port(
    sbc_name: str,
    port_type: str,
    device: str,
    alias: Optional[str] = None,
    baud_rate: int = 115200,
) -> str:
    """Assign a serial port to an SBC.

    Args:
        sbc_name: Name of the SBC
        port_type: Type of port: console, jtag, or debug
        device: Device path (e.g., /dev/lab/port-1)
        alias: Human-friendly name for this connection (e.g., jetson-console)
        baud_rate: Baud rate (default: 115200)
    """
    from labctl.core.models import PortType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    # The own-board check heartbeats, so it runs last: a refused call
    # mustn't extend the caller's claim.
    try:
        port_kind = PortType(port_type)
    except ValueError as e:  # before the claim check, which heartbeats
        return f"Error: {e}"
    claim_err = _check_shared_hardware(
        manager, sbc_name, serial_device_path=device
    ) or _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=port_kind,
            device_path=device,
            baud_rate=baud_rate,
            alias=alias,
        )
        return (
            f"Assigned {port_type} port to {sbc_name}: {device} (tcp:{port.tcp_port})"
        )
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["assign_power_plug"])
@_with_mcp_activity
def assign_power_plug(
    sbc_name: str,
    plug_type: str,
    address: str,
    index: int = 1,
) -> str:
    """Assign a smart power plug to an SBC for remote power control.

    Args:
        sbc_name: Name of the SBC
        plug_type: Type of plug: tasmota, kasa, or shelly
        address: IP address or hostname of the plug
        index: Outlet index for multi-outlet strips (default: 1)
    """
    from labctl.core.models import PlugType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if index < 1:  # Tasmota's Power0 switches every relay on the device
        return f"Error: Plug index must be 1 or more, got {index}"
    try:
        plug_kind = PlugType(plug_type)
    except ValueError as e:  # before the claim check, which heartbeats
        return f"Error: {e}"
    claim_err = _check_shared_hardware(
        manager, sbc_name, plug=(plug_type, address, index)
    ) or _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        plug = manager.assign_power_plug(
            sbc_id=sbc.id,
            plug_type=plug_kind,
            address=address,
            plug_index=index,
        )
        idx = f"[{plug.plug_index}]" if plug.plug_index > 1 else ""
        return f"Assigned {plug_type} plug to {sbc_name}: {address}{idx}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["set_network_address"])
@_with_mcp_activity
def set_network_address(
    sbc_name: str,
    address_type: str,
    ip_address: str,
    mac: Optional[str] = None,
    hostname: Optional[str] = None,
) -> str:
    """Set a network address for an SBC (used for ping health checks).

    Args:
        sbc_name: Name of the SBC
        address_type: Type of connection: ethernet or wifi
        ip_address: IP address (e.g., 192.168.1.100)
        mac: MAC address (optional)
        hostname: Hostname (optional)
    """
    from labctl.core.models import AddressType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        addr = manager.set_network_address(
            sbc_id=sbc.id,
            address_type=AddressType(address_type),
            ip_address=ip_address,
            mac_address=mac,
            hostname=hostname,
        )
        return f"Set {address_type} address for {sbc_name}: {addr.ip_address}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Remove / Unassign Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["remove_serial_port"])
@_with_mcp_activity
def remove_serial_port(sbc_name: str, port_type: str = "console") -> str:
    """Remove a serial port assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
        port_type: Type of port to remove: console, jtag, or debug
    """
    from labctl.core.models import PortType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        if manager.remove_serial_port(sbc.id, PortType(port_type)):
            return f"Removed {port_type} port from {sbc_name}"
        return f"No {port_type} port assigned to {sbc_name}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["remove_network_address"])
@_with_mcp_activity
def remove_network_address(sbc_name: str, address_type: str = "ethernet") -> str:
    """Remove a network address from an SBC.

    Args:
        sbc_name: Name of the SBC
        address_type: Type of address to remove: ethernet or wifi
    """
    from labctl.core.models import AddressType

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        if manager.remove_network_address(sbc.id, AddressType(address_type)):
            return f"Removed {address_type} address from {sbc_name}"
        return f"No {address_type} address assigned to {sbc_name}"
    except (ValueError, KeyError) as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["remove_power_plug"])
@_with_mcp_activity
def remove_power_plug(sbc_name: str) -> str:
    """Remove power plug assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    if manager.remove_power_plug(sbc.id):
        return f"Removed power plug from {sbc_name}"
    return f"No power plug assigned to {sbc_name}"


# ---------------------------------------------------------------------------
# Serial Device CRUD Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["add_serial_device"])
@_with_mcp_activity
def add_serial_device(
    name: str,
    usb_path: str,
    vendor: str | None = None,
    model: str | None = None,
    serial_number: str | None = None,
) -> str:
    """Register a USB-serial adapter.

    Args:
        name: Friendly name for the device (e.g., port-1)
        usb_path: USB bus path (e.g., 1-10.1.3)
        vendor: Manufacturer (optional)
        model: Device model (optional)
        serial_number: Device serial number (optional)
    """
    manager = _get_manager()
    try:
        device = manager.create_serial_device(
            name=name,
            usb_path=usb_path,
            vendor=vendor,
            model=model,
            serial_number=serial_number,
        )
        return f"Registered serial device: {device.name} (usb: {device.usb_path})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["remove_serial_device"])
@_with_mcp_activity
def remove_serial_device(name: str) -> str:
    """Unregister a USB-serial adapter.

    Refused while a board someone else has claimed has a port on the
    adapter, by any path that reaches it (its /dev/lab/<name> link, its
    registration, or a path resolving to the same device node).

    Args:
        name: Name of the serial device to remove
    """
    manager = _get_manager()
    device = manager.get_serial_device_by_name(name)
    if not device:
        return f"Error: Serial device '{name}' not found"
    if _get_config().claims.enabled:
        boards = manager.sbcs_sharing_hardware(
            serial_device_path=f"/dev/lab/{device.name}", serial_device_id=device.id
        )
        claim_err = _check_claims_of(manager, boards)
        if claim_err:
            return claim_err

    try:
        manager.delete_serial_device(device.id)
        return f"Removed serial device: {name}"
    except ValueError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# SDWire Device CRUD Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_add"])
@_with_mcp_activity
def sdwire_add(
    name: str,
    serial_number: str,
    device_type: str = "sdwirec",
) -> str:
    """Register an SDWire SD card multiplexer device.

    Args:
        name: Friendly name (e.g., pi5-sdwire)
        serial_number: Device serial number from discovery
        device_type: Type: sdwirec (Realtek) or sdwire (FTDI legacy)
    """
    manager = _get_manager()
    try:
        device = manager.create_sdwire_device(
            name=name,
            serial_number=serial_number,
            device_type=device_type,
        )
        return f"Registered SDWire device: {device.name} ({device.serial_number})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_remove"])
@_with_mcp_activity
def sdwire_remove(name: str) -> str:
    """Unregister an SDWire device.

    Args:
        name: Name of the SDWire device to remove
    """
    manager = _get_manager()
    device = manager.get_sdwire_device_by_name(name)
    if not device:
        return f"Error: SDWire device '{name}' not found"

    try:
        manager.delete_sdwire_device(device.id)
        return f"Removed SDWire device: {name}"
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_assign"])
@_with_mcp_activity
def sdwire_assign(sbc_name: str, device_name: str) -> str:
    """Assign an SDWire device to an SBC.

    Args:
        sbc_name: Name of the SBC
        device_name: Name of the SDWire device
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    device = manager.get_sdwire_device_by_name(device_name)
    if not device:
        return f"Error: SDWire device '{device_name}' not found"
    claim_err = _check_shared_hardware(
        manager, sbc_name, sdwire_device_id=device.id
    ) or _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    try:
        manager.assign_sdwire(sbc.id, device.id)
        return f"Assigned SDWire '{device_name}' to {sbc_name}"
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_unassign"])
@_with_mcp_activity
def sdwire_unassign(sbc_name: str) -> str:
    """Remove SDWire assignment from an SBC.

    Args:
        sbc_name: Name of the SBC
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err

    if manager.unassign_sdwire(sbc.id):
        return f"Removed SDWire assignment from {sbc_name}"
    return f"No SDWire assigned to {sbc_name}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_discover"])
@_with_mcp_activity
def sdwire_discover() -> str:
    """Discover connected SDWire devices.

    Scans for both SDWireC (Realtek) and legacy SDWire (FTDI) devices.
    Returns device serial numbers and types for registration.
    """
    try:
        from labctl.sdwire.controller import discover_sdwire_devices

        devices = discover_sdwire_devices()
        if not devices:
            return "No SDWire devices found"
        return json.dumps(devices, indent=2)
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["serial_discover"])
@_with_mcp_activity
def serial_discover() -> str:
    """Discover connected USB-serial adapters.

    Scans for ttyUSB and ttyACM devices and returns their USB paths,
    vendor, model, and serial number for registration.
    """
    try:
        from labctl.serial.udev import discover_usb_serial

        devices = discover_usb_serial()
        if not devices:
            return "No USB-serial devices found"
        return json.dumps(devices, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("lab://sdwire-devices")
def list_sdwire_devices() -> str:
    """List all registered SDWire SD card multiplexer devices."""
    manager = _get_manager()
    devices = manager.list_sdwire_devices()
    sbcs = manager.list_sbcs()
    assigned = {}
    for sbc in sbcs:
        if sbc.sdwire:
            assigned[sbc.sdwire.id] = sbc.name
    return json.dumps(
        [
            {
                "name": d.name,
                "serial_number": d.serial_number,
                "assigned_to": assigned.get(d.id),
            }
            for d in devices
        ],
        indent=2,
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_to_dut"])
@_with_mcp_activity
def sdwire_to_dut(sbc_name: str) -> str:
    """Switch an SBC's SD card to DUT mode (SBC boots from the SD card).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_dut()
        result = f"SD card switched to DUT: {sbc_name}"
        advisory = _claim_advisory(manager, sbc_name)
        return f"{result}\n\n{advisory}" if advisory else result
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_to_host"])
@_with_mcp_activity
def sdwire_to_host(sbc_name: str, force: bool = False) -> str:
    """Switch an SBC's SD card to host mode (dev machine can read/write the SD card).

    Proceeds only when the SBC's plug reports it off, to prevent SD card bus
    contention: it refuses when the plug reports it on, when the SBC has no
    power plug, or when its state can't be read or is unknown. Use
    force=True to override (e.g. the board is halted but the plug is on,
    or it has no plug and you know it is off).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        force: Override the power-off check
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    guard_err = _sdwire_host_switch_guard_mcp(
        sbc_name, sbc, force=force, allow_force_override=True
    )
    if guard_err:
        return guard_err

    try:
        ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
        ctrl.switch_to_host()
        # Wait until the card is actually readable, so a follow-up call
        # (e.g. sdwire_ls) sees it on the host instead of racing USB
        # enumeration / media detection.
        block_dev = ctrl.wait_for_host()
        msg = f"SD card switched to host: {sbc_name}"
        if block_dev:
            msg += f" (block device: {block_dev})"
        else:
            msg += " (warning: card not readable on the host within 10s)"
        advisory = _claim_advisory(manager, sbc_name)
        return f"{msg}\n\n{advisory}" if advisory else msg
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_update"])
@_with_mcp_activity
def sdwire_update(
    sbc_name: str,
    partition: int,
    copies: list[str] = [],
    renames: list[str] = [],
    deletes: list[str] = [],
    reboot: bool = False,
) -> str:
    """Copy, rename, and/or delete files on a partition on an SBC's SD card.

    Atomic operation: powers the SBC off (if it has a power plug; refuses,
    writing nothing, if it can't be switched off), switches
    SD to host, mounts partition, performs operations (copies first, then
    renames, then deletes), unmounts, switches back to DUT. The SBC is left
    powered off unless reboot=true, which power cycles it (the board's
    stored cycle delay, or 3 s if none is set).

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        partition: Partition number (e.g., 1 for the first partition)
        copies: List of "source:dest" pairs (dest is relative to partition root);
            a source is an uploaded image's bare name (see list_images) or an
            absolute host path inside mcp.allowed_read_paths
        renames: List of "oldname:newname" pairs (both relative to partition root)
        deletes: List of filenames to delete (relative to partition root)
        reboot: Power cycle the SBC after updating (default false: it
            stays off)
    """
    import time

    from labctl.sdwire import SDWireController

    if not copies and not renames and not deletes:
        return "Error: At least one of copies, renames, or deletes is required"

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    for spec in copies:
        if ":" not in spec:
            return f"Error: Invalid copy format '{spec}'. Use source:dest"

    # Parse rename pairs
    rename_pairs = []
    for spec in renames:
        if ":" not in spec:
            return f"Error: Invalid rename format '{spec}'. Use oldname:newname"
        old_name, new_name = spec.split(":", 1)
        rename_pairs.append((old_name, new_name))

    # Host copy sources must be in mcp.allowed_read_paths; they're opened
    # now (and read by descriptor later), before any power or mux change.
    file_pairs, source_names, path_err = _open_copy_sources(copies)
    if path_err:
        return path_err

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)

    try:
        # The relays and the cycle delay the reboot will need are checked
        # before the board is switched off, not after the card has been
        # written.
        reboot_delay = None
        if reboot and sbc.power_plug:
            from labctl.actuators.runtime import (
                ActuationError,
                board_cycle_delay,
                check_power_on,
            )

            try:
                reboot_delay = board_cycle_delay(sbc)
                check_power_on(manager, sbc)
            except (ActuationError, ValueError) as e:
                return f"Error: {e}. Nothing changed."

        # The card must not leave a running board (bus contention):
        # nothing is written if the board can't be switched off.
        from labctl.sdwire.power_guard import power_off_before_host_switch

        try:
            power_off_before_host_switch(sbc)
        except RuntimeError as e:
            return f"Error: {e}."

        ctrl.switch_to_host()
        time.sleep(2)

        result = ctrl.update_files(
            partition,
            file_pairs,
            renames=rename_pairs or None,
            deletes=list(deletes) or None,
            source_names=source_names or None,
        )

        ctrl.switch_to_dut()

        parts = []
        if result["copied"]:
            parts.append(f"Copied: {', '.join(result['copied'])}")
        if result["renamed"]:
            parts.append(f"Renamed: {', '.join(result['renamed'])}")
        if result["deleted"]:
            parts.append(f"Deleted: {', '.join(result['deleted'])}")
        summary = f"Partition {partition}: {'; '.join(parts)}"

        if reboot and sbc.power_plug:
            from labctl.actuators.runtime import (
                ActuationError,
                power_cycle_with_bindings,
            )
            from labctl.power import PowerController

            power_ctrl = PowerController.from_plug(sbc.power_plug)
            try:
                ok = power_cycle_with_bindings(manager, sbc, power_ctrl, reboot_delay)
                failure = None if ok else f"power_on failed on {sbc_name!r}"
            except (ActuationError, RuntimeError) as e:
                failure = str(e)
            if failure is None:
                summary += f". Power cycled {sbc_name}."
            else:
                summary = (
                    f"Error: power cycle failed and {sbc_name} may be off: "
                    f"{failure}. ({summary}, done.)"
                )

        advisory = _claim_advisory(manager, sbc_name)
        return f"{summary}\n\n{advisory}" if advisory else summary
    except RuntimeError as e:
        return f"Error: {e}"
    finally:
        _close_fds(file_pairs)


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_ls"])
@_with_mcp_activity
def sdwire_ls(
    sbc_name: str,
    partition: int,
    path: str = "/",
    recursive: bool = False,
    max_entries: int = 1000,
) -> str:
    """List directory contents on an SBC SD card partition.

    The partition is mounted read-only. A card already on the host is read
    in place and left there. A card on the DUT is switched to the host and
    back only when the SBC is known to be powered off; otherwise the call
    refuses with error "powered_on" or "power_unknown" (no power plug, or
    its state can't be read) and nothing is switched.

    Returns JSON with "entries" and "truncated"; errors are JSON with
    "error" and "message".

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        partition: Partition number (e.g., 1 for the first partition)
        path: Directory on the partition (default: "/")
        recursive: List subdirectories too (default: false)
        max_entries: Stop after this many entries, setting "truncated"
            (default: 1000)
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return _sdwire_read_error("sbc_not_found", f"SBC '{sbc_name}' not found")
    if not sbc.sdwire:
        return _sdwire_read_error("no_sdwire", f"No SDWire assigned to '{sbc_name}'")
    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    needs_switch, refusal = _sdwire_read_access(sbc_name, sbc, ctrl)
    if refusal:
        return refusal
    host_switched = False
    cleanup_error: RuntimeError | None = None
    response: str

    try:
        if needs_switch:
            ctrl.switch_to_host()
            host_switched = True
            ctrl.wait_for_host()
        result = ctrl.list_files(
            partition=partition,
            path=path,
            recursive=recursive,
            max_entries=max_entries,
        )
        payload = {
            "sbc_name": sbc_name,
            "partition": partition,
            "path": path,
            "claim_advisory": _structured_claim_advisory(manager, sbc_name),
            **result,
        }
        response = json.dumps(payload)
    except FileNotFoundError:
        response = _sdwire_read_error("path_not_found", f"Path not found: {path}")
    except PermissionError:
        response = _sdwire_read_error("permission_denied", f"Permission denied: {path}")
    except NotADirectoryError:
        response = _sdwire_read_error("not_a_directory", f"Not a directory: {path}")
    except RuntimeError as e:
        msg = str(e)
        if "Failed to mount" in msg:
            response = _sdwire_read_error("fs_unsupported", msg)
        else:
            response = _sdwire_read_error("read_failed", msg)
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        return _sdwire_read_error(
            "cleanup_failed",
            f"Failed to restore SD card to DUT mode: {cleanup_error}",
            prior_response=json.loads(response),
        )
    return response


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_cat"])
@_with_mcp_activity
def sdwire_cat(
    sbc_name: str,
    partition: int,
    path: str,
    max_bytes: int = 1024 * 1024,
    encoding: str = "text",
) -> str:
    """Read a file from an SBC SD card partition.

    The partition is mounted read-only and symlinks are not followed. A
    card already on the host is read in place and left there. A card on
    the DUT is switched to the host and back only when the SBC is known to
    be powered off; otherwise the call refuses with error "powered_on" or
    "power_unknown" (no power plug, or its state can't be read) and
    nothing is switched.

    Returns JSON with "content", "encoding" and file metadata; errors are
    JSON with "error" and "message" (e.g. "size_limit_exceeded",
    "binary_content": retry with encoding="base64").

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        partition: Partition number (e.g., 1 for the first partition)
        path: File on the partition
        max_bytes: Refuse files larger than this (default: 1048576, 1 MiB)
        encoding: "text" (UTF-8, the default), "base64" or "hex"
    """
    from labctl.sdwire import SDWireController
    from labctl.sdwire.controller import SDWireSymlinkError

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return _sdwire_read_error("sbc_not_found", f"SBC '{sbc_name}' not found")
    if not sbc.sdwire:
        return _sdwire_read_error("no_sdwire", f"No SDWire assigned to '{sbc_name}'")
    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    needs_switch, refusal = _sdwire_read_access(sbc_name, sbc, ctrl)
    if refusal:
        return refusal
    host_switched = False
    cleanup_error: RuntimeError | None = None
    response: str

    try:
        if needs_switch:
            ctrl.switch_to_host()
            host_switched = True
            ctrl.wait_for_host()
        result = ctrl.read_file(
            partition=partition,
            path=path,
            max_bytes=max_bytes,
            encoding=encoding,
        )
        payload = json.dumps(
            {
                "sbc_name": sbc_name,
                "partition": partition,
                "path": path,
                "claim_advisory": _structured_claim_advisory(manager, sbc_name),
                **result,
            }
        )
        response = payload
    except FileNotFoundError:
        response = _sdwire_read_error("path_not_found", f"Path not found: {path}")
    except PermissionError:
        response = _sdwire_read_error("permission_denied", f"Permission denied: {path}")
    except IsADirectoryError:
        response = _sdwire_read_error("not_a_file", f"Not a file: {path}")
    except ValueError as e:
        if str(e) == "binary_content":
            response = _sdwire_read_error(
                "binary_content",
                f"File is not valid UTF-8: {path}",
                suggestion="Retry with encoding='base64'",
            )
        else:
            response = _sdwire_read_error("read_failed", str(e))
    except SDWireSymlinkError as e:
        response = _sdwire_read_error(
            "not_a_file",
            f"Refusing to follow symlink: {path}",
            symlink_target=e.target,
        )
    except RuntimeError as e:
        msg = str(e)
        if "exceeds max_bytes" in msg:
            response = _sdwire_read_error("size_limit_exceeded", msg)
        elif "Failed to mount" in msg:
            response = _sdwire_read_error("fs_unsupported", msg)
        else:
            response = _sdwire_read_error("read_failed", msg)
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        return _sdwire_read_error(
            "cleanup_failed",
            f"Failed to restore SD card to DUT mode: {cleanup_error}",
            prior_response=json.loads(response),
        )
    return response


@mcp.tool(annotations=TOOL_ANNOTATIONS["sdwire_info"])
@_with_mcp_activity
def sdwire_info(sbc_name: str) -> str:
    """Return partition-table and filesystem metadata for an SBC SD card.

    Reads the partition table (parted); nothing is mounted. A card already
    on the host is read in place and left there. A card on the DUT is
    switched to the host and back only when the SBC is known to be powered
    off; otherwise the call refuses with error "powered_on" or
    "power_unknown" (no power plug, or its state can't be read) and nothing
    is switched. Errors are JSON with "error" and "message".

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
    """
    from labctl.sdwire import SDWireController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return _sdwire_read_error("sbc_not_found", f"SBC '{sbc_name}' not found")
    if not sbc.sdwire:
        return _sdwire_read_error("no_sdwire", f"No SDWire assigned to '{sbc_name}'")
    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    needs_switch, refusal = _sdwire_read_access(sbc_name, sbc, ctrl)
    if refusal:
        return refusal
    host_switched = False
    cleanup_error: RuntimeError | None = None
    response: str

    try:
        if needs_switch:
            ctrl.switch_to_host()
            host_switched = True
            ctrl.wait_for_host()
        result = ctrl.get_disk_info()
        payload = json.dumps(
            {
                "sbc_name": sbc_name,
                "claim_advisory": _structured_claim_advisory(manager, sbc_name),
                **result,
            }
        )
        response = payload
    except RuntimeError as e:
        response = _sdwire_read_error("read_failed", str(e))
    finally:
        if host_switched:
            try:
                ctrl.switch_to_dut()
            except RuntimeError as e:
                cleanup_error = e

    if cleanup_error:
        return _sdwire_read_error(
            "cleanup_failed",
            f"Failed to restore SD card to DUT mode: {cleanup_error}",
            prior_response=json.loads(response),
        )
    return response


@mcp.tool(annotations=TOOL_ANNOTATIONS["flash_image"])
@_with_mcp_activity
def flash_image(
    sbc_name: str,
    image_path: str,
    reboot: bool = False,
    post_flash_copies: list[str] = [],
) -> str:
    """Flash a raw disk image to an SBC's SD card via SDWire.

    Supports .img, .img.xz, and .img.gz formats. Includes safety checks:
    verifies block device size, ensures no partitions are mounted, and
    resolves the device from SDWire config (never accepts raw /dev paths).

    Powers the SBC off first (if it has a power plug; refuses, writing
    nothing, if it can't be switched off) and leaves it off unless
    reboot=true.

    Args:
        sbc_name: Name of the SBC with an assigned SDWire device
        image_path: The image (.img, .img.xz, .img.gz): an uploaded image's
            bare name (see list_images), or an absolute host path inside
            mcp.allowed_read_paths
        reboot: Power on the SBC after flashing (default false: it stays
            off)
        post_flash_copies: Optional "source:dest" pairs to copy to boot partition after flash;
            sources as for image_path
    """  # noqa: E501
    import time as time_mod

    from labctl.sdwire import SDWireController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.sdwire:
        return f"Error: No SDWire assigned to '{sbc_name}'"

    # Host paths must be in mcp.allowed_read_paths. Checked and opened
    # before powering off or switching the mux, so a refusal touches
    # nothing; the data is then read from these descriptors, never by path.
    image_fd, path_err = _open_host_read(image_path)
    if path_err:
        return path_err
    post_flash_pairs, post_flash_names, path_err = _open_copy_sources(post_flash_copies)
    if path_err:
        os.close(image_fd)
        return path_err

    ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
    flash_ok = False

    try:
        # The relays the reboot will need are checked before the board is
        # switched off, not after the card has been written.
        if reboot and sbc.power_plug:
            from labctl.actuators.runtime import ActuationError, check_power_on

            try:
                check_power_on(manager, sbc)
            except ActuationError as e:
                return f"Error: {e}. Nothing changed."

        # The card must not leave a running board (bus contention):
        # nothing is flashed if the board can't be switched off.
        from labctl.sdwire.power_guard import power_off_before_host_switch

        try:
            power_off_before_host_switch(sbc)
        except RuntimeError as e:
            return f"Error: {e}."

        # Switch SD to host and wait for device
        ctrl.switch_to_host()
        time_mod.sleep(2)

        block_dev = ctrl.get_block_device(settle_time=2)
        if not block_dev:
            block_dev = ctrl.get_block_device(settle_time=5)
        if not block_dev:
            return "Error: Block device not found after switching to host (waited 10s)"

        # Flash image (includes safety validation)
        # Read the checked, already-open file; the format follows the name
        # the caller gave (latest.img.xz -> build-4711 is still xz).
        result = ctrl.flash_image(image_path, image_fd=image_fd)
        flash_ok = True

        parts = [
            f"Flashed {image_path} to {result['block_device']}",
            f"{result['bytes_written']} bytes in {result['elapsed_seconds']}s",
        ]

        # Post-flash partition copies
        if post_flash_copies:
            try:
                import subprocess

                subprocess.run(
                    ["sudo", "partprobe", block_dev],
                    capture_output=True,
                    timeout=10,
                )
                time_mod.sleep(2)

                for spec in post_flash_copies:
                    if ":" not in spec:
                        parts.append(f"Skipped invalid copy: {spec}")

                if post_flash_pairs:
                    copied = ctrl.update_files(
                        1, post_flash_pairs, source_names=post_flash_names
                    )
                    for f in copied["copied"]:
                        parts.append(f"Post-flash copied: {f}")
            except RuntimeError as e:
                parts.append(f"Post-flash copy error: {e}")

        # Switch back to DUT
        ctrl.switch_to_dut()

        # Reboot
        if reboot and sbc.power_plug:
            from labctl.actuators.runtime import (
                ActuationError,
                power_on_with_bindings,
            )
            from labctl.power import PowerController

            power_ctrl = PowerController.from_plug(sbc.power_plug)
            try:
                ok = power_on_with_bindings(manager, sbc, power_ctrl)
                failure = None if ok else f"power_on failed on {sbc_name!r}"
            except (ActuationError, RuntimeError) as e:
                failure = str(e)
            if failure is None:
                parts.append(f"Powered on {sbc_name}")
            else:
                parts.insert(0, f"Error: {sbc_name} not powered on: {failure}")

        result = ". ".join(parts)
        advisory = _claim_advisory(manager, sbc_name)
        return f"{result}\n\n{advisory}" if advisory else result

    except RuntimeError as e:
        if not flash_ok:
            return f"Error: {e}. SD card left on host for inspection."
        try:
            ctrl.switch_to_dut()
        except Exception:
            pass
        return f"Error: {e}"
    finally:
        os.close(image_fd)
        _close_fds(post_flash_pairs)


# ---------------------------------------------------------------------------
# Serial I/O Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["serial_capture"])
@_with_mcp_activity
def serial_capture(
    port_name: str,
    timeout: float = 15.0,
    until_pattern: str | None = None,
    tail: int | None = None,
) -> str:
    """Capture serial output from a port.

    Connects to the serial port via ser2net, captures output until timeout
    or pattern match, then disconnects.

    Args:
        port_name: Port alias or SBC name (SBC defaults to console port)
        timeout: Max seconds to capture (default: 15)
        until_pattern: Regex to stop on when matched (per line)
        tail: Return only last N lines (None = all)
    """
    from labctl.serial.capture import (
        capture_serial_output,
        resolve_port,
    )

    manager = _get_manager()

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        return f"Error: {e}"

    # Capturing holds the board's ser2net session, so it is claim-checked
    # like serial_send.
    if port.sbc_id:
        sbc = manager.get_sbc(port.sbc_id)
        if sbc:
            claim_err = _check_claim(manager, sbc.name, mutating=True)
            if claim_err:
                return claim_err

    if not port.tcp_port:
        return f"Error: Port '{port_name}' has no TCP port configured"

    try:
        result = capture_serial_output(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            timeout=timeout,
            until_pattern=until_pattern,
            tail=tail,
        )
        return result.to_mcp_string(pattern=until_pattern)
    except RuntimeError as e:
        return f"Error: {e}"


def _serial_send_payload(
    data: str, newline: bool | None, keys: list[str] | None, data_hex: str | None
) -> list[bytes]:
    """The parts serial_send writes (one per named key, with a short pause
    between them); ValueError for bad keys or hex."""
    from labctl.serial.keys import decode_hex, encode_keys

    if data_hex is not None:
        if data or keys:
            raise ValueError("data_hex can't be combined with data or keys")
        parts = [decode_hex(data_hex)]
    else:
        parts = ([data.encode("utf-8")] if data else []) + encode_keys(keys or [])
    text_only = data_hex is None and keys is None
    if newline is None:
        newline = text_only
    parts = [p for p in parts if p]
    if newline:
        if parts and text_only:
            parts[-1] += b"\r\n"  # text and its line end in one write
        else:
            parts.append(b"\r\n")
    return parts


def _serial_send_preview(arguments: dict) -> dict:
    """The exact bytes a serial_send call will write, for its confirmation."""
    raw = b"".join(
        _serial_send_payload(
            arguments.get("data", ""),
            arguments.get("newline"),
            arguments.get("keys"),
            arguments.get("data_hex"),
        )
    )
    return {"bytes_hex": raw.hex(" "), "byte_count": len(raw)}


_CONFIRM_PREVIEWS["serial_send"] = _serial_send_preview


def _cycle_delay_preview(arguments: dict) -> dict | None:
    """The delay a power-cycling call will wait, for its confirmation.

    The board's stored delay, or 3 s, is the minimum (board_cycle_delay); an
    out-of-range stored or requested delay refuses the call (ValueError).
    None when the call doesn't power-cycle or the board isn't found (the
    tool reports that)."""
    if arguments.get("reboot") is False:
        return None
    sbc = _get_manager().get_sbc_by_name(arguments.get("sbc_name"))
    if sbc is None or not sbc.power_plug:
        return None
    from labctl.actuators.runtime import board_cycle_delay

    return {"cycle_delay_seconds": board_cycle_delay(sbc, arguments.get("delay"))}


for _tool in (
    "power_cycle",
    "enter_recovery",
    "exit_recovery",
    "sdwire_update",
    "boot_test",
):
    _CONFIRM_PREVIEWS[_tool] = _cycle_delay_preview


@mcp.tool(annotations=TOOL_ANNOTATIONS["serial_send"])
@_with_mcp_activity
def serial_send(
    port_name: str,
    data: str = "",
    newline: bool | None = None,
    capture_timeout: float | None = None,
    capture_until: str | None = None,
    keys: list[str] | None = None,
    data_hex: str | None = None,
) -> str:
    """Send data to a serial port, optionally capturing the response.

    Connects to the serial port via ser2net, sends data, optionally
    captures the response, then disconnects.

    Control characters can't be typed into ``data`` (they are lost before
    they reach the server): name keys in ``keys`` instead, e.g. ["F11"]
    for the UEFI boot menu, ["Down", "Down", "Enter"], ["Ctrl-C"] to stop
    U-Boot autoboot, or give raw bytes as ``data_hex``.

    Args:
        port_name: Port alias or SBC name (SBC defaults to console port)
        data: Text to send
        newline: Append \\r\\n at the end (default: True for data alone,
            False when keys or data_hex is given)
        capture_timeout: If set, capture response for this many seconds
        capture_until: If set, capture until this regex matches a line
        keys: Named keys sent after data, in order, with a short pause
            between keys (case-insensitive): ESC, Enter, Tab, Space,
            Backspace (0x08), Up, Down, Left, Right, Home, End, Insert,
            Delete, PgUp, PgDn, F1-F12, Ctrl-A..Ctrl-Z (or Ctrl+C, ^C).
            xterm encodings (F11 = ESC [ 2 3 ~), not yet checked against
            real firmware. Function keys differ by terminal type: if one
            is ignored, find the firmware's terminal type and send its
            sequence as data_hex (e.g. EDK2 VT100+ F11 = ESC !, "1b21").
            Backspace is 0x08 (U-Boot, EDK2); Linux ttys usually erase
            with 0x7F (data_hex "7f").
        data_hex: Raw bytes as hex, e.g. "1b5b32337e" (spaces allowed);
            not combined with data or keys
    """
    from labctl.serial.capture import (
        resolve_port,
        send_serial_data,
    )

    try:
        payload = _serial_send_payload(data, newline, keys, data_hex)
    except ValueError as e:
        return f"Error: {e}"

    manager = _get_manager()

    try:
        port = resolve_port(manager, port_name)
    except ValueError as e:
        return f"Error: {e}"

    # Resolve port → SBC for claim enforcement
    sbc = manager.get_sbc(port.sbc_id) if port.sbc_id else None
    if sbc:
        claim_err = _check_claim(manager, sbc.name, mutating=True)
        if claim_err:
            return claim_err

    if not port.tcp_port:
        return f"Error: Port '{port_name}' has no TCP port configured"

    try:
        result = send_serial_data(
            tcp_host="localhost",
            tcp_port=port.tcp_port,
            data=payload,
            newline=False,
            capture_timeout=capture_timeout,
            capture_until=capture_until,
        )
        result_str = result.to_mcp_string()
        if sbc:
            advisory = _claim_advisory(manager, sbc.name)
            return f"{result_str}\n\n{advisory}" if advisory else result_str
        return result_str
    except RuntimeError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Boot Test
# ---------------------------------------------------------------------------


@mcp.tool(annotations=TOOL_ANNOTATIONS["boot_test"])
@_with_mcp_activity
def boot_test(
    sbc_name: str,
    expect_pattern: str,
    runs: int = 10,
    timeout: float = 30.0,
    image: str | None = None,
    dest: str | None = None,
    partition: int = 1,
    output_dir: str | None = None,
) -> str:
    """Automated boot reliability testing.

    Optionally deploys an image (the SBC is powered off first; nothing is
    deployed if it can't be), then reboots the SBC multiple times
    (waiting the board's stored cycle delay, or 3 s, between off and on),
    capturing serial output each time. Reports how many boots
    successfully reached the expected pattern.

    Args:
        sbc_name: Name of the SBC to test
        expect_pattern: Regex that indicates successful boot
        runs: Number of boot cycles (default: 10)
        timeout: Seconds to wait per boot (default: 30)
        image: File to deploy (None = skip deploy): an uploaded image's bare
            name (see list_images), or an absolute host path inside
            mcp.allowed_read_paths
        dest: Destination filename on SD card (required with image)
        partition: Partition number for deploy (default: 1)
        output_dir: Save per-run output to files here; must be inside
            mcp.allowed_write_paths
    """
    if runs < 1:
        return "Error: runs must be at least 1"
    if timeout <= 0:
        return "Error: timeout must be positive"

    from labctl.serial.boot_test import run_boot_test

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.console_port or not sbc.console_port.tcp_port:
        return f"Error: SBC '{sbc_name}' has no console port with TCP configured"
    if not sbc.power_plug:
        return f"Error: SBC '{sbc_name}' has no power plug assigned"

    if image and not dest:
        return "Error: dest is required when image is specified"

    # Every run power-cycles through the pre_power relays and waits the
    # board's cycle delay: check both before deploying anything.
    from labctl.actuators.runtime import (
        ActuationError,
        board_cycle_delay,
        check_power_on,
    )

    try:
        cycle_delay = board_cycle_delay(sbc)
        check_power_on(manager, sbc)
    except (ActuationError, ValueError) as e:
        return f"Error: {e}. Nothing changed."

    # Host paths: the image is read, per-run output is written (D012).
    # Both are checked and opened now; I/O then goes through these
    # descriptors, never the paths. `dest` is a path on the SD card.
    image_fd = output_dir_fd = None
    if image:
        image_fd, path_err = _open_host_read(image)
        if path_err:
            return path_err
    if output_dir:
        output_dir_fd, output_dir, path_err = _open_host_write_dir(output_dir)
        if path_err:
            if image_fd is not None:
                os.close(image_fd)
            return path_err

    # Build deploy function
    deploy_fn = None
    if image and dest:

        def _deploy():
            from labctl.sdwire import SDWireController

            if not sbc.sdwire:
                raise RuntimeError(f"No SDWire assigned to '{sbc_name}'")
            from labctl.sdwire.power_guard import deploy_to_card

            ctrl = SDWireController(sbc.sdwire.serial_number, sbc.sdwire.device_type)
            # The card must not leave a running board (bus contention):
            # the board is switched off first, and nothing is deployed if
            # that fails. The first run powers it back on.
            deploy_to_card(
                manager,
                sbc,
                ctrl,
                lambda: ctrl.update_files(
                    partition,
                    [(image_fd, dest)],
                    source_names=[os.path.basename(image)],
                ),
            )

        deploy_fn = _deploy

    # Build power cycle function
    from labctl.actuators.runtime import ActuationError, power_cycle_with_bindings

    def power_cycle_fn():
        from labctl.power import PowerController

        power_ctrl = PowerController.from_plug(sbc.power_plug)
        try:
            ok = power_cycle_with_bindings(manager, sbc, power_ctrl, cycle_delay)
        except ActuationError as e:
            # Recorded as a failed run, like a plug failure.
            raise RuntimeError(str(e)) from e
        if not ok:
            raise RuntimeError(f"power_on failed on {sbc_name!r}")

    try:
        result = run_boot_test(
            sbc_name=sbc_name,
            expect_pattern=expect_pattern,
            tcp_host="localhost",
            tcp_port=sbc.console_port.tcp_port,
            power_cycle_fn=power_cycle_fn,
            runs=runs,
            timeout=timeout,
            deploy_fn=deploy_fn,
            image=image,
            dest=dest,
            partition=partition,
            output_dir=output_dir,
            output_dir_fd=output_dir_fd,
        )
        result_str = result.format_summary()
        advisory = _claim_advisory(manager, sbc_name)
        return f"{result_str}\n\n{advisory}" if advisory else result_str
    except RuntimeError as e:
        return f"Error: {e}"
    finally:
        for fd in (image_fd, output_dir_fd):
            if fd is not None:
                os.close(fd)


# ---------------------------------------------------------------------------
# Uploaded images (D014)
# ---------------------------------------------------------------------------
#
# A tool's arguments are text the model writes out, so a disk image can't
# travel in one. Clients upload with their own shell to the MCP HTTP server
# (PUT /images/<name>, the same API key) into mcp.upload_dir; tools then
# take the bare name. labctl.core.images has the rules.


def _image_store():
    """The store for mcp.upload_dir, or None when uploads are off."""
    from labctl.core.images import ImageStore

    upload_dir = _get_config().mcp.upload_dir
    if not upload_dir:
        return None
    return ImageStore(upload_dir, _get_manager().db)


def _image_caller():
    from labctl.core.images import Caller

    return Caller(user=_authenticated_user(), operator=_caller_is_operator())


_UPLOADS_OFF = "Error: image uploads are off on this server (mcp.upload_dir is not set)"


def _stdio_host_path_route() -> str:
    """How a stdio session uses an image that is already on the lab host."""
    read_paths = ", ".join(_get_config().mcp.allowed_read_paths) or "(none set)"
    return (
        "Pass the image's absolute host path to flash_image, sdwire_update "
        "or boot_test; it must be inside mcp.allowed_read_paths "
        f"({read_paths}). To use a build directory in place, ask the user "
        "to add it to mcp.allowed_read_paths in config.yaml and restart "
        "this MCP server (the config is read when it starts)."
    )


def _stdio_image_routes() -> list[str]:
    """How a stdio session, which has no upload route, gets an image in.

    stdio servers usually run on the lab host as the user who started
    them, so the client's shell can use host files directly. The copy and
    HTTP routes end in a bare name, so they are offered only when bare
    names resolve here (upload_dir set and inside allowed_read_paths).
    """
    routes = [_stdio_host_path_route()]
    upload_dir = _get_config().mcp.upload_dir
    if _resolve_host_path(upload_dir, "read")[1]:
        return routes
    if os.access(upload_dir, os.W_OK | os.X_OK):
        access = "this session can create files there"
    else:
        access = "this session can't write there"
    routes.append(
        f"Copy the image with your shell into {upload_dir} ({access}) and "
        "pass its bare name. Copy to a unique temp name in the same "
        "directory and rename it into place, so no session reads a "
        "half-written image, and remove the temp file if the copy fails "
        "(set name to the image name): "
        "name=sdcard.img; "
        f"t=$(mktemp {shlex.quote(upload_dir + '/.upload-XXXXXX')}) && "
        f'{{ cp build/sdcard.img "$t" && chmod 644 "$t" && mv "$t" '
        f'{shlex.quote(upload_dir)}/"$name"; }} || rm -f "$t". '
        "Check free space first (df); this copy isn't counted by the "
        "upload free-space checks. Pick a "
        "name not in list_images, since one copied over replaces that image "
        "for everyone. Names are 1-128 letters, digits, '.', '_', '+' or "
        "'-', starting with a letter or digit. Images put there this way "
        "have no recorded uploader, so only operators may replace or delete "
        "them through labctl."
    )
    routes.append(
        "If the lab host also runs the HTTP MCP service (labctl mcp --http) "
        f"with the same upload_dir ({upload_dir}), you can PUT the image to "
        "its /images/<name> route instead; this session can't tell its URL "
        "or whether it needs an API key, so ask the user for both."
    )
    return routes


def _upload_instructions() -> dict:
    if _SESSION_KIND != "mcp-http":
        return {
            "available": False,
            "message": "This stdio session has no upload route (that is on "
            "the HTTP transport). These alternatives assume your shell is on "
            "the lab host, as it is when the client starts labctl mcp locally "
            "(not over ssh).",
            "alternatives": _stdio_image_routes(),
        }
    auth = (
        ' -H "Authorization: Bearer $LABCTL_API_KEY"'
        if mcp._token_verifier is not None
        else ""
    )
    scheme = "https" if _http_tls else "http"
    return {
        "available": True,
        "method": "PUT",
        "url": "<your MCP URL without /mcp>/images/<name>",
        "example": (
            "curl --fail-with-body -T build/sdcard.img" + auth + " "
            f'"{scheme}://lab.example.com:8080/images/sdcard.img'
            '?sha256=$(sha256sum build/sdcard.img | cut -d" " -f1)"'
        ),
        "notes": [
            "Use the scheme, host and port of the MCP URL your client is "
            "configured with, and the same API key.",
            "sha256 is optional; when given, a mismatch stores nothing.",
            "Add overwrite=1 to replace an image you uploaded (operators: "
            "any image).",
            "Then pass the bare name to flash_image, sdwire_update copies "
            "or boot_test.",
        ],
    }


@mcp.tool(annotations=TOOL_ANNOTATIONS["list_images"])
@_with_mcp_activity
def list_images() -> str:
    """List the images uploaded to this server (mcp.upload_dir), and how to
    upload one.

    Files can't be sent in tool arguments: upload them with your shell to
    this MCP server over HTTP (the response's ``upload`` section has the
    URL and a curl example), then pass the bare name (e.g.
    "sdcard.img") as flash_image's image_path, an sdwire_update copy
    source ("zImage:zImage") or boot_test's image. stdio sessions have no
    upload route; they run on the lab host and can pass an absolute host
    path inside mcp.allowed_read_paths instead (the ``upload`` section
    lists the alternatives).
    """
    from labctl.core.images import ImageError

    store = _image_store()
    if store is None:
        if _SESSION_KIND != "mcp-http":
            return _UPLOADS_OFF + ". " + _stdio_host_path_route()
        return _UPLOADS_OFF
    try:
        images = store.list()
    except ImageError as e:
        if _SESSION_KIND != "mcp-http":
            return f"Error: {e.message}. {_stdio_host_path_route()}"
        return f"Error: {e.message}"
    max_mb = _get_config().mcp.max_upload_mb
    return json.dumps(
        {
            "upload_dir": store.directory,
            "max_upload_mb": max_mb,
            "images": images,
            "upload": _upload_instructions(),
        },
        indent=2,
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["delete_image"])
@_with_mcp_activity
def delete_image(name: str) -> str:
    """Delete an uploaded image from mcp.upload_dir.

    Only the user who uploaded it, or an operator, may delete it. A tool
    already reading the image (a flash in progress) keeps reading it.

    Args:
        name: The image's bare name, as list_images shows it
    """
    from labctl.core import audit
    from labctl.core.images import ImageError

    store = _image_store()
    if store is None:
        return _UPLOADS_OFF
    try:
        store.delete(name, _image_caller())
    except ImageError as e:
        audit.emit(
            store.db,
            action="image_delete",
            entity_type="image",
            entity_name=name,
            result="forbidden" if e.status == 403 else "error",
            details={"error": e.code},
        )
        return f"Error: {e.message}"
    except Exception as e:  # noqa: BLE001 - an error result and an audit row
        logger.exception("Deleting image %r failed", name)
        audit.emit(
            store.db,
            action="image_delete",
            entity_type="image",
            entity_name=name,
            result="error",
            details={"error": type(e).__name__},
        )
        return f"Error: could not delete image '{name}': {e}"
    audit.emit(store.db, action="image_delete", entity_type="image", entity_name=name)
    return f"Deleted image: {name}"


# Host/Origin checks for the upload route: the SDK applies
# transport_security to /mcp only. run_server sets this to what /mcp gets;
# _UNSET (a server built another way) checks as for a loopback bind.
_UNSET = object()
_http_security = _UNSET
# Whether the HTTP transport serves TLS (for the upload instructions).
_http_tls = False


def _upload_security():
    if _http_security is _UNSET:
        from labctl.core.config import McpConfig

        return _transport_security("127.0.0.1", McpConfig())
    return _http_security


_TRUE_WORDS = ("1", "true", "yes", "on")
# Body bytes buffered before each write to disk (one thread hop per write).
_UPLOAD_WRITE_BYTES = 4 << 20


@mcp.custom_route("/images/{name}", methods=["PUT"])
async def upload_image_route(request):
    """PUT /images/<name>: store the body as an image in mcp.upload_dir.

    The SDK serves custom routes without its auth check, so this checks
    Host/Origin and the API key itself, as /mcp does. Query: overwrite=1
    to replace an existing image (its uploader or an operator), sha256=HEX
    to verify the data before it's stored. Content-Length is required.
    """
    import anyio
    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import ClientDisconnect
    from starlette.responses import JSONResponse

    from labctl.core import audit
    from labctl.core.images import ImageError

    def refuse(code: str, status: int, message: str, **headers) -> JSONResponse:
        return JSONResponse(
            {"error": code, "message": message}, status_code=status, headers=headers
        )

    security = _upload_security()
    if security is not None:
        refused = await TransportSecurityMiddleware(security).validate_request(
            request, is_post=False
        )
        if refused is not None:
            return refused
    # The bearer middleware runs for every route when auth is on, but only
    # /mcp requires a user; require one here too.
    if mcp._token_verifier is not None and _authenticated_user() is None:
        return refuse(
            "unauthorized",
            401,
            "send 'Authorization: Bearer <api_key>'",
            **{"WWW-Authenticate": "Bearer"},
        )
    try:
        store = await anyio.to_thread.run_sync(_image_store)
        if store is None:
            return refuse("uploads_disabled", 403, _UPLOADS_OFF[len("Error: ") :])
        length = request.headers.get("content-length")
        if length is None or not (length.isascii() and length.isdigit()):
            return refuse(
                "length_required",
                411,
                "Content-Length is required (curl -T FILE sends it)",
            )
        name = request.path_params["name"]
        query = request.query_params
        overwrite = query.get("overwrite", "").strip().lower() in _TRUE_WORDS
        caller = _image_caller()
        actor = _get_session_id()
        max_bytes = _get_config().mcp.max_upload_mb << 20
    except Exception as e:  # noqa: BLE001 - JSON, as for every other error
        # Before any audit context exists (e.g. the database won't open).
        logger.exception("Image upload setup failed")
        return refuse(
            "upload_failed", 500, f"could not start the upload: {type(e).__name__}"
        )

    def record(result: str, details: dict) -> None:
        audit.emit(
            store.db,
            action="image_upload",
            entity_type="image",
            entity_name=name,
            result=result,
            details=details,
            actor=actor,
            source="mcp",
        )

    upload = None
    try:
        upload = await anyio.to_thread.run_sync(
            lambda: store.begin_upload(
                name,
                int(length),
                caller,
                overwrite=overwrite,
                max_bytes=max_bytes,
                expected_sha256=query.get("sha256"),
            )
        )
        pending = bytearray()
        async for chunk in request.stream():
            pending += chunk
            if len(pending) >= _UPLOAD_WRITE_BYTES:
                data, pending = pending, bytearray()
                await anyio.to_thread.run_sync(upload.write, data)
        if pending:
            await anyio.to_thread.run_sync(upload.write, pending)
        result = await anyio.to_thread.run_sync(upload.commit)
    except ImageError as e:
        failure = (e, "forbidden" if e.status == 403 else "error", e.code)
    except ClientDisconnect:
        failure = (
            ImageError("disconnected", 400, "the client disconnected mid-upload"),
            "error",
            "disconnected",
        )
    except Exception as e:  # noqa: BLE001 - a JSON error and an audit row
        logger.exception("Image upload %r failed", name)
        failure = (
            ImageError(
                "upload_failed",
                500,
                f"could not store the image: {getattr(e, 'strerror', None) or e}",
            ),
            "error",
            type(e).__name__,
        )
    else:
        failure = None
    finally:
        if upload is not None:
            # Shielded: a cancelled request (shutdown, disconnect) must
            # still drop its temp file and free-space reservation.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(upload.abort)
    if failure is not None:
        error, outcome, code = failure
        await anyio.to_thread.run_sync(
            record, outcome, {"error": code, "size": int(length)}
        )
        return JSONResponse(error.to_dict(), status_code=error.status)
    await anyio.to_thread.run_sync(
        record,
        "ok",
        {
            "size": result["size"],
            "sha256": result["sha256"],
            "replaced": result["replaced"],
        },
    )
    return JSONResponse(result, status_code=200 if result["replaced"] else 201)


# ---------------------------------------------------------------------------
# Claims (exclusive access coordination)
# ---------------------------------------------------------------------------


@mcp.resource("lab://claims")
def list_claims_resource() -> str:
    """All active claims with their metadata."""
    manager = _get_manager()
    claims = manager.list_active_claims()
    return json.dumps([c.to_dict() for c in claims], indent=2)


@mcp.resource("lab://claims/{sbc_name}")
def get_claim_resource(sbc_name: str) -> str:
    """Current claim on a specific SBC."""
    manager = _get_manager()
    claim = manager.get_active_claim(sbc_name)
    if claim is None:
        return json.dumps({"sbc_name": sbc_name, "claimed": False})
    return json.dumps({"sbc_name": sbc_name, "claimed": True, "claim": claim.to_dict()})


@mcp.resource("lab://claims/history/{sbc_name}")
def get_claim_history_resource(sbc_name: str) -> str:
    """Historical claims (released) for an SBC."""
    manager = _get_manager()
    history = manager.list_claim_history(sbc_name, limit=20)
    return json.dumps([c.to_dict() for c in history], indent=2)


@mcp.resource("lab://claims/metrics")
def get_claim_metrics_resource() -> str:
    """Aggregate claim statistics: totals by outcome, average duration."""
    manager = _get_manager()
    return json.dumps(manager.get_claim_metrics(), indent=2)


@mcp.resource("lab://activity/recent")
def get_recent_activity_resource() -> str:
    """Recent activity events across the lab."""
    manager = _get_manager()
    return json.dumps(audit.query_events(manager.db, limit=50), indent=2)


@mcp.resource("lab://activity/{sbc_name}")
def get_activity_for_sbc_resource(sbc_name: str) -> str:
    """Recent activity events for a specific SBC."""
    manager = _get_manager()
    return json.dumps(
        audit.query_events(manager.db, limit=50, sbc=sbc_name),
        indent=2,
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["claim_sbc"])
@_with_mcp_activity
def claim_sbc(
    sbc_name: str,
    duration_minutes: int = 30,
    reason: str = "",
    agent_name: str | None = None,
    context: dict | None = None,
) -> str:
    """Claim exclusive access to an SBC for a declared duration.

    While claimed, mutating operations by other agents are blocked.

    Args:
        sbc_name: Name of the SBC to claim
        duration_minutes: How long to hold (default 30; must be within
            claims.min_duration_minutes..max_duration_minutes)
        reason: Human-readable reason, recorded in the audit log
            (optional; "no reason given" when omitted)
        agent_name: Self-declared agent identifier
        context: Optional metadata (git branch, ticket, etc.)
    """
    from labctl.core.models import (
        ClaimConflict,
        ClaimError,
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    if agent_name:
        _set_agent_name(agent_name)

    config = _get_config()
    duration_s = duration_minutes * 60
    min_s = config.claims.min_duration_minutes * 60
    max_s = config.claims.max_duration_minutes * 60
    if duration_s < min_s or duration_s > max_s:
        return json.dumps(
            {
                "error": "duration_out_of_bounds",
                "message": (
                    f"Duration {duration_minutes}m is outside "
                    f"[{config.claims.min_duration_minutes}m, "
                    f"{config.claims.max_duration_minutes}m]"
                ),
            }
        )

    manager = _get_manager()
    claim_args = dict(
        sbc_name=sbc_name,
        agent_name=_get_agent_name(),
        session_id=_get_session_id(),
        session_kind=_SESSION_KIND,
        duration_seconds=duration_s,
        reason=reason or "no reason given",
        context=context,
        grace_seconds=config.claims.grace_period_seconds,
        owner=_authenticated_user(),
    )

    def shared_hardware_refusal():
        """Refuse a new claim the board's own holder or another user's
        claim on a board sharing its hardware stops (see
        ResourceManager.new_claim_conflict)."""
        if not config.claims.enabled:
            return None
        claim = manager.new_claim_conflict(
            sbc_name, _caller_actor(), config.claims.grace_period_seconds
        )
        return _claimed_error(sbc_name, claim) if claim is not None else None

    def grant():
        refusal = shared_hardware_refusal()
        if refusal:
            return None, refusal
        return manager.claim_sbc(**claim_args), None

    try:
        # Your own active claim is renewed (below) without re-checking
        # boards that share its hardware; anything else is a new claim.
        open_claim = manager.get_open_claim(sbc_name)
        if (
            open_claim is not None
            and open_claim.is_active
            and _caller_actor().holds(open_claim)
        ):
            raise ClaimConflict(open_claim)
        claim, shared_err = grant()
        if shared_err:
            return shared_err
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    except ClaimConflict as exc:
        if not _caller_actor().holds(exc.claim):
            return _claimed_error(sbc_name, exc.claim)
        # Already yours (e.g. from your previous session or a server
        # restart): renew it for the duration asked, so a claim adopted
        # with minutes left doesn't lapse mid-task.
        try:
            renewed = manager.renew_claim(
                sbc_name,
                _get_session_id(),
                duration_seconds=duration_s,
                owner=_authenticated_user(),
            )
        except ClaimNotFoundError:
            # It lapsed between the two calls: the board is free now.
            try:
                claim, shared_err = grant()
            except ClaimConflict as again:
                return _claimed_error(sbc_name, again.claim)
            except ClaimError as claim_exc:
                return json.dumps({"error": "claim_failed", "message": str(claim_exc)})
            if shared_err:
                return shared_err
            return json.dumps(
                {"status": "claimed", "sbc_name": sbc_name, "claim": claim.to_dict()}
            )
        except NotClaimantError:
            # It lapsed and someone else claimed the board meanwhile.
            current = manager.get_active_claim(sbc_name)
            if current is not None:
                return _claimed_error(sbc_name, current)
            return json.dumps(
                {"error": "renew_failed", "message": "Claim changed; retry"}
            )
        except ClaimError as renew_exc:
            return json.dumps({"error": "renew_failed", "message": str(renew_exc)})
        return json.dumps(
            {
                "status": "already_claimed",
                "sbc_name": sbc_name,
                "claim": renewed.to_dict(),
                "message": (
                    "You already held this claim; it now runs for the "
                    "requested duration (its original reason is kept)."
                ),
            }
        )

    return json.dumps(
        {"status": "claimed", "sbc_name": sbc_name, "claim": claim.to_dict()}
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["release_sbc"])
@_with_mcp_activity
def release_sbc(sbc_name: str) -> str:
    """Release a claim held by the calling session.

    Only the claimant can release it via this tool: the session that
    created the claim or, over authenticated HTTP, the same user.

    Args:
        sbc_name: Name of the SBC to release
    """
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    manager = _get_manager()
    try:
        manager.release_claim(
            sbc_name,
            _get_session_id(),
            owner=_authenticated_user(),
            released_by=_get_agent_name(),
        )
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    except ClaimNotFoundError:
        return json.dumps(
            {
                "error": "claim_not_found",
                "message": f"No active claim on '{sbc_name}'",
            }
        )
    except NotClaimantError:
        return json.dumps(
            {
                "error": "not_claimant",
                "message": f"You don't hold the claim on '{sbc_name}'",
            }
        )

    return json.dumps({"status": "released", "sbc_name": sbc_name})


@mcp.tool(annotations=TOOL_ANNOTATIONS["renew_sbc_claim"])
@_with_mcp_activity
def renew_sbc_claim(sbc_name: str, duration_minutes: int | None = None) -> str:
    """Extend an active claim. Bounded by config max_duration.

    Args:
        sbc_name: Name of the SBC whose claim to renew
        duration_minutes: New duration (omit to keep current)
    """
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    config = _get_config()
    duration_s = None
    if duration_minutes is not None:
        duration_s = duration_minutes * 60
        max_s = config.claims.max_duration_minutes * 60
        if duration_s > max_s:
            return json.dumps(
                {
                    "error": "duration_out_of_bounds",
                    "message": (
                        f"Duration {duration_minutes}m exceeds "
                        f"max {config.claims.max_duration_minutes}m"
                    ),
                }
            )

    manager = _get_manager()
    try:
        claim = manager.renew_claim(
            sbc_name,
            _get_session_id(),
            duration_seconds=duration_s,
            owner=_authenticated_user(),
        )
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    except ClaimNotFoundError:
        return json.dumps(
            {
                "error": "claim_not_found",
                "message": f"No active claim on '{sbc_name}'",
            }
        )
    except NotClaimantError:
        return json.dumps(
            {
                "error": "not_claimant",
                "message": f"You don't hold the claim on '{sbc_name}'",
            }
        )

    return json.dumps(
        {"status": "renewed", "sbc_name": sbc_name, "claim": claim.to_dict()}
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["list_claims"])
@_with_mcp_activity
def list_claims() -> str:
    """List all active claims across the lab."""
    manager = _get_manager()
    claims = manager.list_active_claims()
    return json.dumps({"active_claims": [c.to_dict() for c in claims]}, indent=2)


@mcp.tool(annotations=TOOL_ANNOTATIONS["get_claim"])
@_with_mcp_activity
def get_claim(sbc_name: str) -> str:
    """Get the current active claim on an SBC, including pending requests.

    Args:
        sbc_name: Name of the SBC to check
    """
    from labctl.core.models import UnknownSBCError

    manager = _get_manager()
    try:
        claim = manager.get_active_claim(sbc_name)
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    if claim is None:
        return json.dumps({"sbc_name": sbc_name, "claimed": False})
    return json.dumps({"sbc_name": sbc_name, "claimed": True, "claim": claim.to_dict()})


@mcp.tool(annotations=TOOL_ANNOTATIONS["request_sbc_release"])
@_with_mcp_activity
def request_sbc_release(sbc_name: str, reason: str) -> str:
    """Politely ask the current claimant to release an SBC.

    Non-binding — the claimant decides whether to act.

    Args:
        sbc_name: Name of the SBC
        reason: Why you need the SBC
    """
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    manager = _get_manager()
    try:
        manager.record_release_request(
            sbc_name, requested_by=_get_agent_name(), reason=reason
        )
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    except ClaimNotFoundError:
        return json.dumps(
            {
                "error": "claim_not_found",
                "message": f"No active claim on '{sbc_name}'",
            }
        )

    return json.dumps({"status": "request_recorded", "sbc_name": sbc_name})


@mcp.tool(annotations=TOOL_ANNOTATIONS["force_release_sbc"])
@_with_mcp_activity
def force_release_sbc(sbc_name: str, reason: str) -> str:
    """Operator override — forcibly release an active claim.

    Should only be used when normal release is blocked (dead agent,
    emergency).

    Args:
        sbc_name: Name of the SBC
        reason: Why the override is needed (logged prominently)
    """
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    if not _caller_is_operator():
        return json.dumps(
            {
                "error": "not_operator",
                "sbc_name": sbc_name,
                "message": (
                    "Only operators may force-release a claim; your user "
                    "has role 'agent'."
                ),
                "hints": ["Ask the holder with request_sbc_release"],
            }
        )

    manager = _get_manager()
    try:
        released = manager.force_release_claim(
            sbc_name, reason, released_by=_authenticated_user() or _get_agent_name()
        )
    except UnknownSBCError:
        return json.dumps(
            {"error": "unknown_sbc", "message": f"SBC '{sbc_name}' not found"}
        )
    except ClaimNotFoundError:
        return json.dumps(
            {
                "error": "claim_not_found",
                "message": f"No active claim on '{sbc_name}'",
            }
        )

    return json.dumps(
        {
            "status": "force_released",
            "sbc_name": sbc_name,
            "was_held_by": released.agent_name,
        }
    )


# ---------------------------------------------------------------------------
# Actuator tools (Phase 7)
# ---------------------------------------------------------------------------
#
# Tool layering matches docs/SPEC_actuators.md:
#
#   actuator_*    — provisioning. Privileged: these are the tools that
#                   could let an agent bypass the binding layer
#                   (raw set_channel writes). Surface them but document
#                   the risk; transport-level scoping is the operator's
#                   responsibility for v1.
#   bind / unbind — config management. Mutates DB only.
#   bindings_*    — read-only inspection.
#   actuate / release / press
#                 — the binding-mediated verbs. Claim-gated on the
#                   target SBC, like every other mutating SBC operation.
#   enter_recovery / exit_recovery
#                 — power-aware composites. Claim-gated.


def _actuator_to_dict(actuator) -> dict:
    return actuator.to_dict(include_ids=False)


def _binding_to_dict(binding) -> dict:
    return binding.to_dict(include_ids=False)


def _admin_actuator_ops_allowed() -> bool:
    """Whether the privileged actuator_* tools are enabled in config.

    Defaults to False — operators flip mcp.allow_admin_actuator_ops in
    config.yaml when running MCP behind a trusted transport. Direct
    `actuator_set` would otherwise let an agent bypass the binding
    layer's safety checks (verb/shape congruence, polarity, etc.).
    """
    try:
        return bool(_get_config().mcp.allow_admin_actuator_ops)
    except Exception as e:  # noqa: BLE001
        # Fail closed: any config-load error is treated as "gate disabled".
        # Logged at debug so operators have a breadcrumb if all admin tools
        # silently refuse work because config.yaml is malformed.
        logger.debug(
            "admin_actuator_ops gate defaulting to False (config load failed): %s",
            e,
        )
        return False


# Allow-list of device-path prefixes for ``actuator_add`` over MCP.
# Operators can extend by symlinking custom paths under one of these.
_ACTUATOR_DEVICE_PATH_PREFIXES = (
    "/dev/lab/",
    "/dev/serial/by-id/",
    "/dev/ttyUSB",
    "/dev/ttyACM",
)


def _validate_actuator_device_path(device_path: str) -> Optional[str]:
    """Return an error message if ``device_path`` is suspicious, else None.

    A hostile MCP caller could try to point ``actuator_add`` at
    ``/dev/disk/...``, ``/etc/...``, etc. The allow-list keeps the
    surface area to USB-serial paths labctl actually uses.

    Two layers of defense against path-traversal tricks:
      * Reject any input containing ``..`` outright. Real USB-serial
        paths never contain it; an attacker passing
        ``/dev/ttyUSB../disk/foo`` (where ``ttyUSB..`` is a single
        component, so normpath doesn't collapse it) is stopped here.
      * Normalize before the allow-list check so
        ``/dev/lab/../etc/passwd`` collapses to ``/etc/passwd`` and
        fails the prefix match.
    """
    if not device_path:
        return None  # device_path is optional; nothing to validate
    if ".." in device_path:
        return (
            f"device_path {device_path!r} contains '..' "
            f"(path traversal not allowed)"
        )
    normalized = os.path.normpath(device_path)
    if any(normalized.startswith(p) for p in _ACTUATOR_DEVICE_PATH_PREFIXES):
        return None
    return (
        f"device_path {device_path!r} not under any of the allowed "
        f"prefixes: {', '.join(_ACTUATOR_DEVICE_PATH_PREFIXES)}"
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuator_list"])
@_with_mcp_activity
def actuator_list() -> str:
    """List provisioned actuators (read-only)."""
    manager = _get_manager()
    return json.dumps(
        [_actuator_to_dict(a) for a in manager.list_actuators()],
        indent=2,
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuator_probe"])
@_with_mcp_activity
def actuator_probe(name: str) -> str:
    """Probe an actuator for reachability (read-only).

    Records last_probe_at / last_probe_result on the actuator row.

    Args:
        name: Actuator name.
    """
    from labctl.actuators.runtime import probe_actuator

    manager = _get_manager()
    actuator = manager.get_actuator_by_name(name)
    if not actuator:
        return f"Error: actuator '{name}' not found"
    outcome = probe_actuator(actuator)
    manager.record_actuator_probe(actuator.id, outcome.result.value)
    return json.dumps(
        {"actuator": name, "result": outcome.result.value, "detail": outcome.detail}
    )


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuator_add"])
@_with_mcp_activity
def actuator_add(
    name: str,
    driver: str,
    device_path: Optional[str] = None,
    vid: Optional[str] = None,
    pid: Optional[str] = None,
    serial_no: Optional[str] = None,
    channels: int = 1,
) -> str:
    """Register a new actuator (PRIVILEGED — gated by config).

    Privileged: requires ``mcp.allow_admin_actuator_ops: true`` in
    ``/etc/labctl/config.yaml``. Direct access lets an agent bypass the
    binding layer with raw `actuator_set` writes; the gate keeps that
    behind an explicit operator opt-in.

    ``device_path`` is validated against an allow-list of USB-serial
    prefixes (``/dev/lab/``, ``/dev/serial/by-id/``, ``/dev/ttyUSB*``,
    ``/dev/ttyACM*``) to refuse obviously dodgy targets.

    Args:
        name: Stable actuator name (e.g. "usbrelay-rack1-a").
        driver: Driver implementation. v1: "lcus1_serial".
        device_path: Device path hint (e.g. "/dev/lab/relay-rack1-a").
        vid, pid, serial_no: Optional USB identity hints.
        channels: Number of channels on the device (default 1).
    """
    from labctl.actuators import get_driver
    from labctl.core.models import ChannelState, DriverName

    if not _admin_actuator_ops_allowed():
        return (
            "Error: actuator_add is disabled. Set "
            "mcp.allow_admin_actuator_ops=true in config.yaml to enable."
        )
    if channels < 1:
        return "Error: channels must be at least 1"
    try:
        driver_name = DriverName(driver)
    except ValueError:
        return f"Error: unknown driver {driver!r}"

    path_err = _validate_actuator_device_path(device_path)
    if path_err is not None:
        return f"Error: {path_err}"

    try:
        probe = get_driver(driver_name, expected_channel_count=channels)
    except NotImplementedError as e:
        return f"Error: {e}"

    enumerated = probe.channel_count()
    if enumerated is not None and enumerated != channels:
        return (
            f"Error: driver enumerated {enumerated} channels but "
            f"channels={channels} disagrees"
        )

    manager = _get_manager()
    actuator = manager.create_actuator(
        name,
        driver_name,
        device_path=device_path,
        vid=vid,
        pid=pid,
        serial_no=serial_no,
    )
    for i in range(1, channels + 1):
        manager.add_actuator_channel(actuator.id, i, default_state=ChannelState.OPEN)
    return f"Added actuator {name!r} ({driver}, {channels} channel(s))"


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuator_remove"])
@_with_mcp_activity
def actuator_remove(name: str) -> str:
    """Remove an actuator and any bindings on its channels (PRIVILEGED — gated).

    Requires ``mcp.allow_admin_actuator_ops: true`` in config.yaml.

    Args:
        name: Actuator name.
    """
    if not _admin_actuator_ops_allowed():
        return (
            "Error: actuator_remove is disabled. Set "
            "mcp.allow_admin_actuator_ops=true in config.yaml to enable."
        )
    manager = _get_manager()
    actuator = manager.get_actuator_by_name(name)
    if not actuator:
        return f"Error: actuator '{name}' not found"
    claim_err = _check_bound_claims(
        manager, {c.id for c in actuator.channels}, same_device=False
    )
    if claim_err:
        return claim_err
    manager.delete_actuator(actuator.id)
    return f"Removed actuator {name!r}"


def _check_bound_claims(
    manager,
    channel_ids: set,
    *,
    same_device: bool = True,
    for_sbc: str | None = None,
) -> str | None:
    """Claim-check every SBC bound to one of these actuator channels: a
    relay wired to a claimed board's reset or power is part of that board.
    ``same_device`` also covers other actuator records for the same relay
    board (needed when driving a channel, not when deleting a record)."""
    if not _get_config().claims.enabled:
        return None
    names = set(manager.sbcs_bound_to_channels(channel_ids, same_device=same_device))
    names.discard(for_sbc)
    return _check_claims_of(manager, names, for_sbc=for_sbc)


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuator_set"])
@_with_mcp_activity
def actuator_set(name: str, channel: int, state: str) -> str:
    """Drive a channel directly, bypassing bindings (PRIVILEGED — gated).

    Requires ``mcp.allow_admin_actuator_ops: true`` in config.yaml.
    Production paths should use `actuate` / `release` / `press` against
    bound channels — those go through verb/shape congruence and
    polarity validation. ``actuator_set`` is only for wiring
    verification.

    Args:
        name: Actuator name.
        channel: Channel index (1-based).
        state: "open" or "closed".
    """
    from labctl.actuators import DriverError, get_driver
    from labctl.core.models import ChannelState

    if not _admin_actuator_ops_allowed():
        return (
            "Error: actuator_set is disabled. Set "
            "mcp.allow_admin_actuator_ops=true in config.yaml to enable."
        )
    try:
        target_state = ChannelState(state)
    except ValueError:
        return f"Error: state must be 'open' or 'closed', got {state!r}"

    manager = _get_manager()
    actuator = manager.get_actuator_by_name(name)
    if not actuator:
        return f"Error: actuator '{name}' not found"
    ch = manager.get_actuator_channel(actuator.id, channel)
    if not ch:
        return f"Error: channel {channel} not on {name!r}"
    claim_err = _check_bound_claims(manager, {ch.id})
    if claim_err:
        return claim_err

    try:
        driver = get_driver(
            actuator.driver, expected_channel_count=len(actuator.channels)
        )
    except NotImplementedError as e:
        return f"Error: {e}"

    from labctl.actuators import Transport

    transport = Transport(
        device_path=actuator.device_path,
        vid=actuator.vid,
        pid=actuator.pid,
        serial_no=actuator.serial_no,
    )
    from labctl.actuators.runtime import _audit_raw_actuator_set

    try:
        driver.open(transport)
        outcome = driver.set_channel(
            channel, closed=target_state is ChannelState.CLOSED
        )
    except DriverError as e:
        return f"Error: {e}"
    finally:
        driver.close()

    if outcome.value != "ok":
        _audit_raw_actuator_set(
            manager,
            actuator,
            ch,
            target_state,
            ok=False,
            error=outcome.value,
        )
        return f"Error: set_channel returned {outcome.value}"
    manager.update_channel_state(ch.id, target_state)
    _audit_raw_actuator_set(manager, actuator, ch, target_state, ok=True)
    return f"Set {name}[{channel}] -> {state}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["bind"])
@_with_mcp_activity
def bind(
    sbc_name: str,
    purpose: str,
    actuator: str,
    channel: int,
    mode: str,
    active_when: str = "closed",
    pulse_ms: Optional[int] = None,
    sample_phase: str = "none",
    notes: Optional[str] = None,
) -> str:
    """Create a binding linking an actuator channel to (SBC, purpose).

    Args:
        sbc_name: Target SBC.
        purpose: e.g. "recovery_mode", "boot_select", "power_button".
        actuator: Actuator name.
        channel: Channel index (1-based).
        mode: "latch" or "momentary".
        active_when: "closed" or "open" — physical state == "asserted".
        pulse_ms: Required when mode="momentary".
        sample_phase: "pre_power", "post_power", or "none".
        notes: Optional free-text note (wiring photo URL, etc.).
    """
    from labctl.core.models import ChannelState, SamplePhase, ShapeMode

    try:
        shape_mode = ShapeMode(mode)
        shape_active = ChannelState(active_when)
        phase = SamplePhase(sample_phase)
    except ValueError as e:
        return f"Error: {e}"

    if shape_mode is ShapeMode.MOMENTARY and pulse_ms is None:
        return "Error: pulse_ms is required when mode='momentary'"
    if shape_mode is ShapeMode.LATCH and pulse_ms is not None:
        return "Error: pulse_ms only applies to momentary bindings"

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    a = manager.get_actuator_by_name(actuator)
    if not a:
        return f"Error: actuator '{actuator}' not found"
    ch = manager.get_actuator_channel(a.id, channel)
    if not ch:
        return f"Error: channel {channel} not on {actuator!r}"
    if ch.default_state == shape_active:
        return (
            f"Error: default_state={ch.default_state.value} and "
            f"active_when={shape_active.value}; the device would always "
            "be asserted. Verify wiring."
        )

    # A channel takes one binding; if a claimed board has it, say so
    # rather than fail on the constraint. Own-board check (heartbeats) last.
    claim_err = _check_bound_claims(manager, {ch.id}, for_sbc=sbc_name) or _check_claim(
        manager, sbc_name, shared=False
    )
    if claim_err:
        return claim_err

    binding = manager.create_binding(
        sbc.id,
        purpose,
        ch.id,
        shape_mode=shape_mode,
        shape_active=shape_active,
        momentary_pulse_ms=pulse_ms,
        sample_phase=phase,
        notes=notes,
    )
    return json.dumps({"bound": _binding_to_dict(binding)})


@mcp.tool(annotations=TOOL_ANNOTATIONS["unbind"])
@_with_mcp_activity
def unbind(sbc_name: str, purpose: str) -> str:
    """Remove a binding.

    Args:
        sbc_name: Target SBC.
        purpose: Binding purpose to remove.
    """
    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    claim_err = _check_claim(manager, sbc_name, shared=False)
    if claim_err:
        return claim_err
    binding = manager.get_binding_by_target(sbc.id, purpose)
    if not binding:
        return f"Error: no binding for {sbc_name}:{purpose}"
    manager.delete_binding(binding.id)
    return f"Unbound {sbc_name}:{purpose}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["bindings_list"])
@_with_mcp_activity
def bindings_list(sbc_name: Optional[str] = None) -> str:
    """List bindings (read-only), optionally filtered by SBC.

    Args:
        sbc_name: Filter to one SBC, or omit for all.
    """
    manager = _get_manager()
    sbc_id = None
    if sbc_name:
        sbc = manager.get_sbc_by_name(sbc_name)
        if not sbc:
            return f"Error: SBC '{sbc_name}' not found"
        sbc_id = sbc.id
    bindings = manager.list_bindings(sbc_id=sbc_id)
    return json.dumps([_binding_to_dict(b) for b in bindings], indent=2)


def _run_binding_verb(sbc_name: str, purpose: str, fn) -> str:
    """Shared error handling for actuate / release / press tools."""
    from labctl.actuators.runtime import (
        ActuationError,
        BindingShapeError,
        ChannelBusyError,
    )

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    binding = manager.get_binding_by_target(sbc.id, purpose)
    if not binding:
        return f"Error: no binding for {sbc_name}:{purpose}"

    try:
        fn(manager, binding)
    except BindingShapeError as e:
        return f"Error (shape): {e}"
    except ChannelBusyError as e:
        return f"Error (busy): {e}"
    except ActuationError as e:
        return f"Error (actuation): {e}"
    return f"OK: {sbc_name}:{purpose}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuate"])
@_with_mcp_activity
def actuate(sbc_name: str, purpose: str) -> str:
    """Drive a latch binding to its active state (sets desired=asserted).

    Args:
        sbc_name: Target SBC.
        purpose: Binding purpose, e.g. "recovery_mode".
    """
    from labctl.actuators.runtime import actuate_binding

    return _run_binding_verb(sbc_name, purpose, actuate_binding)


@mcp.tool(annotations=TOOL_ANNOTATIONS["release"])
@_with_mcp_activity
def release(sbc_name: str, purpose: str) -> str:
    """Drive a latch binding away from active (sets desired=released).

    Args:
        sbc_name: Target SBC.
        purpose: Binding purpose.
    """
    from labctl.actuators.runtime import release_binding

    return _run_binding_verb(sbc_name, purpose, release_binding)


@mcp.tool(annotations=TOOL_ANNOTATIONS["press"])
@_with_mcp_activity
def press(sbc_name: str, purpose: str) -> str:
    """Pulse a momentary binding for its configured pulse_ms.

    Args:
        sbc_name: Target SBC.
        purpose: Binding purpose, e.g. "power_button".
    """
    from labctl.actuators.runtime import press_binding

    return _run_binding_verb(sbc_name, purpose, press_binding)


@mcp.tool(annotations=TOOL_ANNOTATIONS["actuation_status"])
@_with_mcp_activity
def actuation_status(sbc_name: str, purpose: str) -> str:
    """Return desired vs. last state for a binding (read-only).

    Args:
        sbc_name: Target SBC.
        purpose: Binding purpose.
    """
    from labctl.actuators.runtime import binding_status

    manager = _get_manager()
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    binding = manager.get_binding_by_target(sbc.id, purpose)
    if not binding:
        return f"Error: no binding for {sbc_name}:{purpose}"
    return json.dumps(binding_status(manager, binding), indent=2)


def _run_recovery_composite(sbc_name: str, fn_name: str, fn) -> str:
    from labctl.actuators.runtime import ActuationError, board_cycle_delay
    from labctl.power import PowerController

    manager = _get_manager()
    claim_err = _check_claim(manager, sbc_name, mutating=True)
    if claim_err:
        return claim_err
    sbc = manager.get_sbc_by_name(sbc_name)
    if not sbc:
        return f"Error: SBC '{sbc_name}' not found"
    if not sbc.power_plug:
        return f"Error: no power plug on '{sbc_name}'"

    try:
        delay = board_cycle_delay(sbc)
    except ValueError as e:  # an out-of-range stored delay; nothing touched
        return f"Error: {fn_name} refused: {e}"
    try:
        controller = PowerController.from_plug(sbc.power_plug)
        fn(manager, sbc, controller, delay_s=delay)
    except (ActuationError, RuntimeError) as e:
        return f"Error: {fn_name} failed: {e}"
    return f"OK: {fn_name} {sbc_name}"


@mcp.tool(annotations=TOOL_ANNOTATIONS["enter_recovery"])
@_with_mcp_activity
def enter_recovery(sbc_name: str) -> str:
    """Power-aware "enter recovery mode" composite.

    Pre-flight probes the recovery_mode actuator, then power_off →
    sleep → engage strap → power_on. Aborts before any power
    transition if the actuator is unreachable. The sleep is the SBC's
    stored power-cycle delay (3.0 s if none is set).

    Args:
        sbc_name: Target SBC.
    """
    from labctl.actuators.runtime import enter_recovery as _enter

    return _run_recovery_composite(sbc_name, "enter_recovery", _enter)


@mcp.tool(annotations=TOOL_ANNOTATIONS["exit_recovery"])
@_with_mcp_activity
def exit_recovery(sbc_name: str) -> str:
    """Power-aware "leave recovery mode" composite (release strap, normal boot).

    Like enter_recovery: probes the recovery_mode actuator, then
    power_off → sleep → release strap → power_on. The sleep is the SBC's
    stored power-cycle delay (3.0 s if none is set).

    Args:
        sbc_name: Target SBC.
    """
    from labctl.actuators.runtime import exit_recovery as _exit

    return _run_recovery_composite(sbc_name, "exit_recovery", _exit)


# ---------------------------------------------------------------------------
# Prompts (reusable instruction templates)
# ---------------------------------------------------------------------------


@mcp.prompt()
def debug_sbc(sbc_name: str) -> str:
    """Guide through debugging an unresponsive SBC.

    Args:
        sbc_name: Name of the SBC to debug
    """
    return f"""Debug SBC '{sbc_name}' by following these steps:

1. Check the SBC's current state by reading the lab://sbcs/{sbc_name} resource.
2. Check power state by reading the lab://power/{sbc_name} resource.
3. Run a health check using the run_health_check tool with sbc_name="{sbc_name}".

Based on findings:
- If power is OFF: use the power_on tool to turn it on, wait 30 seconds, then re-check health.
- If power is ON but ping fails: the SBC may be hung. Try power_cycle to reboot it.
- If ping succeeds but serial fails: check physical serial cable connections.
- If everything passes but status is wrong: use update_sbc to correct the status.

Report your findings and any actions taken."""  # noqa: E501


@mcp.prompt()
def lab_report() -> str:
    """Generate a comprehensive lab status report."""
    return """Generate a lab status report by:

1. Read the lab://status resource for an overview of all SBCs.
2. Read the lab://serial-devices resource for registered USB adapters.
3. Read the lab://ports resource for serial port assignments.

Compile a report with:
- Summary: total SBCs, how many online/offline/unknown
- Per-SBC details: name, project, status, IP, power state
- Any issues: SBCs that are offline or in error state
- Serial port utilization: assigned vs unassigned devices
- Recommendations for any problems found"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _release_session_claims():
    """Best-effort release of claims held by this MCP session on exit."""
    try:
        manager = _get_manager()
        for claim in manager.list_active_claims():
            if _is_process_session(claim.session_id):
                try:
                    manager.release_claim(claim.sbc_name, claim.session_id)
                    logger.info(
                        "Released claim on '%s' (session exit)",
                        claim.sbc_name,
                    )
                except Exception:
                    pass
    except Exception:
        pass


def _start_expiry_thread(interval: int = 30):
    """Start a daemon thread that periodically sweeps expired/dead claims."""
    import threading

    def _sweep_loop():
        while True:
            import time as _t

            _t.sleep(interval)
            try:
                config = _get_config()
                manager = _get_manager()
                grace = config.claims.grace_period_seconds
                expired = manager.expire_stale_claims(grace_seconds=grace)
                dead = manager.release_dead_sessions(grace_seconds=grace)
                pruned = manager.prune_released_claims(
                    older_than_days=config.claims.auto_prune_released_after_days
                )
                activity_pruned = manager.prune_activity_events(older_than_days=30)
                if expired or dead or pruned or activity_pruned:
                    logger.info(
                        "Claim sweep: %d expired, %d dead-session, "
                        "%d claims pruned, %d activity events pruned",
                        expired,
                        dead,
                        pruned,
                        activity_pruned,
                    )
            except Exception:
                logger.debug("Claim sweep error", exc_info=True)

    t = threading.Thread(target=_sweep_loop, daemon=True, name="claim-expiry")
    t.start()
    return t


def _warn_unknown_confirm_exemptions(mcp_cfg=None) -> None:
    """Log mcp.confirm_exempt names that aren't tools (likely typos).

    An unknown name fails safe (the intended tool keeps asking for
    confirmation), but silently; say so once at startup. Pass the startup
    config's ``mcp`` section so the warning describes the same file.
    """
    if mcp_cfg is None:
        mcp_cfg = _get_config().mcp
    unknown = sorted(set(mcp_cfg.confirm_exempt) - set(TOOL_ANNOTATIONS))
    if unknown:
        logger.warning(
            "mcp.confirm_exempt lists unknown tool name(s), ignored: %s",
            ", ".join(unknown),
        )


# ---------------------------------------------------------------------------
# HTTP transport security (D012 WS4)
# ---------------------------------------------------------------------------
#
# HTTP clients authenticate with `Authorization: Bearer <api key>`, where the
# keys are the web users' (auth.users[].api_key), whenever auth.enabled is
# true. Without auth the server only listens on loopback. The SDK's bearer
# middleware does the checking and binds each MCP session to the user that
# created it, so one user can't drive another's session by its ID.

_MIN_API_KEY_LENGTH = 16


class McpStartupError(RuntimeError):
    """The MCP server refuses to start with this configuration."""


def _is_loopback(host: str) -> bool:
    """True when ``host`` is a loopback bind address (hostnames: localhost only)."""
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class _ApiKeyVerifier:
    """SDK token verifier: a bearer token is valid if it is a user's API key.

    Re-reads the startup config file on every request (_fresh_config, not
    the startup snapshot), so removing or rotating a key takes effect
    without a restart; a missing or broken file has no users and fails
    closed, never falling back to another config.
    """

    _config_ok = True
    _reads_started = 0
    _last_reported = 0

    def _report_config_state(self, config, seq: int) -> None:
        """Log when the keys' config file stops (or starts) loading: once
        per change, not per request (requests can be unauthenticated).

        ``seq`` orders the re-reads (they overlap in worker threads): only
        a read newer than the last one reported counts, so a slow read of a
        half-saved file can't log "refusing" after a newer one succeeded.
        Logged whatever log_level says, since it happens only once.
        """
        if seq <= self._last_reported:
            return
        self._last_reported = seq
        ok = not config.load_errors
        if ok == self._config_ok:
            return
        self._config_ok = ok
        with _startup_warnings_visible():
            if ok:
                logger.warning(
                    "API keys: config file loads again (%d user(s) with a key)",
                    len(config.auth.key_users()),
                )
            else:
                logger.warning(
                    "API keys: config not loaded (%s); refusing every key until "
                    "fixed",
                    "; ".join(config.load_errors),
                )

    async def verify_token(self, token: str):
        import anyio
        from mcp.server.auth.provider import AccessToken

        # A fresh load, not _get_config(): while the file is broken there
        # are no users, so every key is refused (fail closed).
        self._reads_started += 1
        seq = self._reads_started
        config = await anyio.to_thread.run_sync(_fresh_config)
        self._report_config_state(config, seq)
        user = config.auth.user_for_api_key(token)
        if user is None:
            return None
        # The role rides in the scopes: _caller_is_operator() reads it.
        return AccessToken(token=token, client_id=user.username, scopes=[user.role])


def _authenticated_user() -> str | None:
    """Username behind the current HTTP request, or None (stdio / no auth)."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    return token.client_id if token is not None else None


def _header_name_host(entry: str, origin: bool) -> str:
    """The host part of an allowed_hosts ("name[:port]") or
    allowed_origins ("scheme://name[:port]") entry."""
    from urllib.parse import urlsplit

    try:
        return urlsplit(entry if origin else f"//{entry}").hostname or entry
    except ValueError:
        return entry


def _non_loopback_header_names(mcp_cfg) -> list[str]:
    """Configured allowed Host/Origin names that aren't loopback."""
    names = [(e, _header_name_host(e, False)) for e in mcp_cfg.allowed_hosts]
    names += [(e, _header_name_host(e, True)) for e in mcp_cfg.allowed_origins]
    return [entry for entry, name in names if not _is_loopback(name)]


def _http_auth_required(host: str, config) -> bool:
    """Decide whether the HTTP transport requires API keys, or refuse to start.

    Raises McpStartupError for a server reachable beyond loopback without
    authentication (a non-loopback bind, or a loopback bind that accepts a
    non-loopback Host/Origin, i.e. sits behind a proxy), and for
    auth.enabled with no usable API key (nobody could connect).
    """
    if config.auth.enabled:
        keyed = config.auth.key_users()
        for index, user in enumerate(config.auth.users):
            if not user.api_key or user in keyed:
                continue
            # Both fields must be strings; YAML loads unquoted digits as int.
            problems = []
            if not isinstance(user.api_key, str):
                problems.append("api_key is not a string (quote it)")
            if not isinstance(user.username, str) or not user.username:
                problems.append("username is missing or not a string (quote it)")
            logger.warning(
                "auth.users[%d] ignored for API key auth: %s",
                index,
                "; ".join(problems),
            )
        if not keyed:
            raise McpStartupError(
                "auth.enabled is true but no user in auth.users has a usable "
                "api_key (a string, with a string username), so no MCP client "
                "could authenticate. Add one (generate with: labctl user "
                "generate-key); see the warnings above for ignored entries."
            )
        for user in keyed:
            if len(user.api_key) < _MIN_API_KEY_LENGTH:
                logger.warning(
                    "API key for user %r is shorter than %d characters; "
                    "use a long random key",
                    user.username,
                    _MIN_API_KEY_LENGTH,
                )
        return True
    how = (
        "Set auth.enabled: true and give users an api_key (clients then send "
        "'Authorization: Bearer <api_key>')"
    )
    if not _is_loopback(host):
        raise McpStartupError(
            f"Refusing to serve MCP over HTTP on {host} without authentication. "
            f"{how}, or bind to 127.0.0.1."
        )
    exposed = _non_loopback_header_names(config.mcp)
    if exposed:
        raise McpStartupError(
            "Refusing to serve MCP over HTTP without authentication while "
            f"mcp.allowed_hosts/allowed_origins accept {', '.join(exposed)}: "
            "a proxy would expose every tool unauthenticated. "
            f"{how}, or remove those entries."
        )
    return False


def _transport_security(host: str, mcp_cfg):
    """Host/Origin header checks (DNS-rebinding protection) for HTTP.

    Loopback binds always accept the loopback names, plus any configured
    ``mcp.allowed_hosts`` / ``allowed_origins`` (e.g. a reverse proxy's
    public name). A non-loopback bind is checked only when hosts are
    configured: its reachable names can't be guessed, and API-key auth is
    mandatory there anyway.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = list(mcp_cfg.allowed_hosts)
    origins = list(mcp_cfg.allowed_origins)
    if _is_loopback(host):
        bind = f"[{host.strip('[]')}]" if ":" in host else host
        for name in dict.fromkeys(["127.0.0.1", "localhost", "[::1]", bind]):
            hosts.append(f"{name}:*")
            origins.append(f"http://{name}:*")
            origins.append(f"https://{name}:*")
    if not hosts:
        logger.warning(
            "mcp.allowed_hosts is not set: Host headers are not checked on %s "
            "(API keys are still required)%s",
            host,
            (
                "; mcp.allowed_origins is ignored without allowed_hosts"
                if origins
                else ""
            ),
        )
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )


def _enable_http_auth() -> None:
    """Make the HTTP app require bearer tokens checked by _ApiKeyVerifier.

    The SDK only takes a token verifier in the MCPServer constructor, but
    `mcp` is built at import (the tool decorators need it) before we know
    the transport, so set the two attributes streamable_http_app() reads.
    test_mcp_server pins this against the SDK with real HTTP requests.
    """
    from mcp.server.auth.settings import AuthSettings

    # issuer_url is mandatory but unused: with no OAuth provider and no
    # resource_server_url the SDK neither serves nor advertises it.
    mcp.settings.auth = AuthSettings(
        issuer_url="http://localhost", resource_server_url=None
    )
    mcp._token_verifier = _ApiKeyVerifier()


@contextmanager
def _startup_warnings_visible():
    """Let startup warnings through even if the config's log_level is
    quieter (e.g. ERROR): they're the misconfigurations an operator needs
    to see in the journal or in `labctl mcp --check`."""
    root = logging.getLogger()
    saved = root.level
    if root.getEffectiveLevel() > logging.WARNING:
        root.setLevel(logging.WARNING)
    try:
        yield
    finally:
        root.setLevel(saved)


def _warn_config_load_errors(config) -> None:
    for error in config.load_errors:
        logger.warning("Config file not loaded, ignored: %s", error)


def check_stdio_startup(config) -> None:
    """The stdio transport's startup checks (warnings only).

    run_server(transport="stdio") runs this before serving, and
    `labctl mcp --check` runs it alone.
    """
    with _startup_warnings_visible():
        _warn_config_load_errors(config)
        _warn_unknown_confirm_exemptions(config.mcp)


def http_tls_files(config, cert: str | None = None, key: str | None = None):
    """The ``(cert_file, key_file)`` to serve HTTP with TLS, or None.

    ``--cert``/``--key`` take precedence over mcp.cert_file/key_file; both
    or neither must be given, and both must be readable files. Raises
    McpStartupError otherwise, before anything is served.
    """
    cert = cert or config.mcp.cert_file or None
    key = key or config.mcp.key_file or None
    if not cert and not key:
        return None
    if not (cert and key):
        raise McpStartupError(
            "TLS needs both a certificate and a key (--cert/--key or "
            "mcp.cert_file/mcp.key_file); only one was given"
        )
    import ssl

    for label, path in (("certificate", cert), ("key", key)):
        try:
            with open(path, "rb"):
                pass
        except OSError as e:
            raise McpStartupError(
                f"TLS {label} {path} can't be read: {e.strerror or e}"
            ) from e
    try:
        # What uvicorn will do, now, so a bad file fails the start (and
        # --check) instead of crashing after "TLS with certificate".
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)
    except (ssl.SSLError, ValueError) as e:
        raise McpStartupError(
            f"TLS certificate {cert} and key {key} don't load as a pair: {e}"
        ) from e
    return cert, key


def _warn_upload_settings(mcp_cfg) -> None:
    """Uploads that tools couldn't use, or that can't be stored."""
    upload_dir = mcp_cfg.upload_dir
    if not upload_dir:
        return
    resolved = os.path.realpath(upload_dir)
    if not os.path.isdir(resolved):
        logger.warning(
            "mcp.upload_dir %s is not a directory: uploads will fail", upload_dir
        )
    roots = [os.path.realpath(r) for r in mcp_cfg.allowed_read_paths]
    if not any(os.path.commonpath([resolved, r]) == r for r in roots):
        logger.warning(
            "mcp.upload_dir %s is outside mcp.allowed_read_paths: tools can't "
            "read what is uploaded there",
            upload_dir,
        )


def check_http_startup(host: str, config, tls=None):
    """The HTTP transport's startup checks, without serving anything.

    run_server(transport="http") runs exactly this before serving, and
    `labctl mcp --http PORT --check` runs it alone. Returns
    ``(auth_required, transport_security)``; raises McpStartupError when
    the server would refuse to start, including when a config file it
    should read can't be loaded: falling back to another file or to
    built-in defaults could mean serving without the authentication the
    intended config enables. Logs the startup warnings (running without
    auth, short keys, ignored users, unchecked Host headers, unknown
    confirm exemptions) whatever the configured log_level.
    """
    if config.load_errors:
        raise McpStartupError(
            "Refusing to serve MCP over HTTP: config not loaded ("
            + "; ".join(config.load_errors)
            + "). Serving on another file or on built-in defaults could "
            "drop the authentication the intended config enables."
        )
    with _startup_warnings_visible():
        required = _http_auth_required(host, config)
        if not required:
            logger.warning(
                "MCP HTTP on %s without authentication (auth.enabled is false); "
                "any local user can call tools. Do not put a reverse proxy in "
                "front without enabling auth: one that forwards Host as "
                "127.0.0.1 can't be detected and would expose every tool",
                host,
            )
        security = _transport_security(host, config.mcp)
        _warn_unknown_confirm_exemptions(config.mcp)
        _warn_upload_settings(config.mcp)
        if required and tls is None and not _is_loopback(host):
            logger.warning(
                "MCP HTTP on %s without TLS: API keys and uploads cross the "
                "network unencrypted. Use --cert/--key (mcp.cert_file/key_file), "
                "a TLS proxy, or an SSH tunnel",
                host,
            )
    return required, security


def run_server(
    transport: str = "stdio",
    http_port: int = 8080,
    host: str = "127.0.0.1",
    cert: str | None = None,
    key: str | None = None,
):
    """Start the MCP server.

    Args:
        transport: "stdio" or "http" (streamable HTTP).
        http_port: TCP port for the HTTP transport.
        host: Bind address for the HTTP transport. Defaults to loopback;
            any other address requires auth.enabled with API keys.
        cert, key: TLS certificate and key files for the HTTP transport
            (default: mcp.cert_file / mcp.key_file; none means plain HTTP).

    Raises:
        McpStartupError: the HTTP settings are unsafe or unusable.
    """
    import atexit

    if transport not in ("stdio", "http"):
        raise ValueError(f"Unknown transport: {transport}")
    _set_transport(transport)

    global _http_security, _http_tls

    security = tls = None
    config = _get_config()
    if transport == "http":
        tls = http_tls_files(config, cert, key)
        required, security = check_http_startup(host, config, tls)
        # On stdout (not the protocol channel over HTTP), whatever log_level
        # says: the service journal shows the auth mode of every start.
        # scripts/update.sh greps these exact strings; keep them in sync.
        print(
            (
                "MCP HTTP: API keys required"
                if required
                else "MCP HTTP: no authentication (loopback only)"
            ),
            flush=True,
        )
        if tls:
            print(f"MCP HTTP: TLS with certificate {tls[0]}", flush=True)
        if required:
            _enable_http_auth()
        _http_security = security
        _http_tls = bool(tls)
    else:
        check_stdio_startup(config)
    # From here on, every load_config() in this process (tools, database,
    # allowlists, confirmation, actuator gate, Kasa, health, the claim
    # sweep) gets this config: edits take effect on restart (update.sh
    # restarts). Only API keys are re-read, from this same file, per
    # request (_ApiKeyVerifier), so revoking a key needs no restart.
    from labctl.core.config import snapshot_config

    snapshot_config(config)

    atexit.register(_release_session_claims)
    _start_expiry_thread(interval=30)

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        _serve_http(host, http_port, security, tls)


def _http_server(host: str, port: int, security, tls):
    """The uvicorn server for the streamable-HTTP app (with the upload
    route), with TLS when ``tls`` is ``(cert_file, key_file)``. The SDK's
    own runner takes no TLS options; this is the same app and server
    config otherwise."""
    import uvicorn

    app = mcp.streamable_http_app(transport_security=security, host=host)
    ssl = {"ssl_certfile": tls[0], "ssl_keyfile": tls[1]} if tls else {}
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=mcp.settings.log_level.lower(),
            **ssl,
        )
    )


# How long the HTTP server waits for a port that is in use before giving
# up. Seen once on a deploy: the new server's bind failed ~1 s after the
# old one had exited (what held the port is unknown), and systemd only
# restarted it after RestartSec. ("MCP server:" messages: update.sh reads
# "MCP HTTP:" lines as the auth mode.)
BIND_RETRY_SECONDS = 15.0


def _bind_sockets(
    host: str,
    port: int,
    *,
    retry_seconds: float = BIND_RETRY_SECONDS,
    sleep=_time_mod.sleep,
    clock=_time_mod.monotonic,
) -> list:
    """Sockets bound to ``host``:``port``, made as asyncio's
    create_server (what uvicorn uses for a host and port) makes them: one
    per getaddrinfo address ("" for all interfaces), SO_REUSEADDR,
    IPV6_V6ONLY on IPv6, skipping an address whose family this host can't
    create or bind (EADDRNOTAVAIL, e.g. IPv6 disabled; Python 3.12's
    asyncio skips it too, 3.10's doesn't, and uvicorn then reports that
    error itself). While an address is
    in use, closes them and tries again, for up to ``retry_seconds``; other
    errors, an address still in use at the end, or no address at all, are
    raised."""
    import socket

    infos = {
        (family, socktype, proto, addr)
        for family, socktype, proto, _, addr in socket.getaddrinfo(
            host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    }
    deadline = clock() + retry_seconds
    announced = False
    while True:
        sockets = []
        try:
            for family, socktype, proto, addr in sorted(infos, key=repr):
                try:
                    sock = socket.socket(family, socktype, proto)
                except OSError:
                    continue  # a family/type this host doesn't support
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(addr)
                except OSError as e:
                    sock.close()
                    if e.errno == errno.EADDRNOTAVAIL:
                        continue  # the family isn't enabled here
                    raise
                sockets.append(sock)
            if not sockets:
                raise OSError(f"could not bind on any address of {host or '*'}:{port}")
            return sockets
        except OSError as e:
            for sock in sockets:
                sock.close()
            if e.errno != errno.EADDRINUSE or clock() >= deadline:
                raise
            if not announced:
                print(
                    f"MCP server: {host}:{port} is in use; retrying for up to "
                    f"{retry_seconds:g}s",
                    flush=True,
                )
                announced = True
            sleep(0.5)


def _wait_for_port(host: str, port: int) -> None:
    """Return once ``host``:``port`` can be bound, as uvicorn will bind it
    (waiting up to BIND_RETRY_SECONDS while it is in use); exit 1 if it
    can't. The probe sockets are closed again (never listening, so nothing
    mistakes them for the server): uvicorn binds its own."""
    try:
        sockets = _bind_sockets(host, port)
    except OSError as e:
        print(f"MCP server: can't listen on {host}:{port}: {e}", file=sys.stderr)
        sys.exit(1)
    for sock in sockets:
        sock.close()


def _serve_http(host: str, port: int, security, tls) -> None:
    # The app first, so uvicorn binds right after the wait.
    server = _http_server(host, port, security, tls)
    _wait_for_port(host, port)
    server.run()


if __name__ == "__main__":
    run_server()
