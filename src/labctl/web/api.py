"""
REST API endpoints for lab controller.
"""

import threading
import time

from flask import Blueprint, g, jsonify, request

from labctl import __version__
from labctl.actuators.runtime import (
    ActuationError,
    PrePowerCheckError,
    board_cycle_delay,
    power_cycle_with_bindings,
    power_on_with_bindings,
)
from labctl.core import audit
from labctl.core.manager import outlets_overlap, plug_outlet_key
from labctl.core.models import PortType, Status
from labctl.power import PowerController
from labctl.web import claims

api_bp = Blueprint("api", __name__)

# Rate limiting and overlap: keyed by plug outlet (type, address, index),
# not board name, since a cycle switches every board on the outlet. A
# cycle blocks its request for the whole delay, so the in-progress marker
# (not the time of the last cycle) is what refuses an overlapping one.
_power_cycle_times: dict[tuple, float] = {}
POWER_CYCLE_MIN_INTERVAL = 5.0  # seconds
_power_cycles_in_progress: set[tuple] = set()
_power_cycle_lock = threading.Lock()


def _outlet_key(sbc) -> tuple:
    return plug_outlet_key(sbc.power_plug)


def outlet_cycle_in_progress(sbc) -> bool:
    """Whether a REST/web power cycle of ``sbc``'s outlet is running in
    this server (``on``/``off`` are refused meanwhile)."""
    key = _outlet_key(sbc)
    with _power_cycle_lock:
        return any(outlets_overlap(key, k) for k in _power_cycles_in_progress)


def begin_power_cycle(sbc) -> str | None:
    """Mark a REST/web power cycle of ``sbc``'s outlet as started, before
    anything is switched; return why it must not start instead (a cycle
    of that outlet is running, or the last one ended under
    POWER_CYCLE_MIN_INTERVAL ago). Pair with :func:`end_power_cycle`."""
    key = _outlet_key(sbc)
    now = time.monotonic()
    with _power_cycle_lock:
        if any(outlets_overlap(key, k) for k in _power_cycles_in_progress):
            return (
                f"a power cycle of {sbc.name!r}'s power outlet is already in "
                "progress"
            )
        last = max(
            (t for k, t in _power_cycle_times.items() if outlets_overlap(key, k)),
            default=float("-inf"),
        )
        elapsed = now - last
        if elapsed < POWER_CYCLE_MIN_INTERVAL:
            wait = POWER_CYCLE_MIN_INTERVAL - elapsed
            return f"Rate limited: wait {wait:.1f}s before next power cycle"
        _power_cycles_in_progress.add(key)
    return None


def end_power_cycle(sbc, success: bool) -> None:
    """The cycle begun with :func:`begin_power_cycle` is over; a
    successful one starts the rate-limit interval."""
    key = _outlet_key(sbc)
    with _power_cycle_lock:
        _power_cycles_in_progress.discard(key)
        if success:
            _power_cycle_times[key] = time.monotonic()


def sbc_to_dict(sbc) -> dict:
    """Convert SBC model to JSON-serializable dict."""
    return sbc.to_dict(include_ids=True)


def _refused(refusal):
    """JSON response for a claim refusal (see labctl.web.claims.check)."""
    body, status = refusal
    return jsonify(body), status


# --- SBC Endpoints ---


@api_bp.route("/sbcs", methods=["GET"])
def list_sbcs():
    """List all SBCs."""
    project = request.args.get("project")
    status = request.args.get("status")

    status_filter = Status(status) if status else None
    sbcs = g.manager.list_sbcs(project=project, status=status_filter)

    return jsonify(
        {
            "sbcs": [sbc_to_dict(sbc) for sbc in sbcs],
            "count": len(sbcs),
        }
    )


@api_bp.route("/sbcs/<name>", methods=["GET"])
def get_sbc(name: str):
    """Get SBC details."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    return jsonify(sbc_to_dict(sbc))


@api_bp.route("/sbcs", methods=["POST"])
def create_sbc():
    """Create new SBC."""
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"error": "name is required"}), 400

    try:
        sbc = g.manager.create_sbc(
            name=data["name"],
            project=data.get("project"),
            description=data.get("description"),
            ssh_user=data.get("ssh_user", "root"),
        )
        return jsonify(sbc_to_dict(sbc)), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/sbcs/<name>", methods=["PUT"])
def update_sbc(name: str):
    """Update SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    data = request.get_json() or {}

    status = None
    if "status" in data:
        try:
            status = Status(data["status"])
        except ValueError:
            return jsonify({"error": f"Invalid status: {data['status']}"}), 400

    refusal = claims.check(sbc.name, shared=False)
    if refusal:
        return _refused(refusal)

    updated = g.manager.update_sbc(
        sbc.id,
        name=data.get("name"),
        project=data.get("project"),
        description=data.get("description"),
        ssh_user=data.get("ssh_user"),
        status=status,
    )

    return jsonify(sbc_to_dict(updated))


@api_bp.route("/sbcs/<name>", methods=["DELETE"])
def delete_sbc(name: str):
    """Delete SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    from labctl.core.models import ClaimConflict, UnknownSBCError

    # Force only past the claim there before the check: the caller's own
    # (it goes with the board; nobody else can claim it meanwhile) or, for
    # an operator's override, the one check() logs (also with claims
    # disabled, when nothing is checked). A claim made after it isn't
    # forced: delete_sbc refuses it below.
    actor = claims.web_actor()
    try:
        current = g.manager.get_active_claim(sbc.name)
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{name}' not found"}), 404
    force = current is not None and (
        actor.holds(current) or (claims.override_requested() and actor.operator)
    )

    refusal = claims.check(sbc.name, shared=False)
    if refusal:
        return _refused(refusal)
    try:
        deleted = g.manager.delete_sbc(sbc.id, force=force)
    except ClaimConflict as exc:
        # Claimed meanwhile, or claims are disabled (nothing is checked,
        # but a claimed board is only deleted with override_claim).
        return jsonify(claims.claimed_error(sbc.name, exc.claim)), 409
    if deleted:
        return jsonify({"message": f"SBC '{name}' deleted"}), 200
    return jsonify({"error": "Failed to delete SBC"}), 500


# --- Power Endpoints ---


@api_bp.route("/sbcs/<name>/power", methods=["GET"])
def get_power_status(name: str):
    """Get power status for SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    if not sbc.power_plug:
        return jsonify({"error": "No power plug assigned"}), 400

    try:
        controller = PowerController.from_plug(sbc.power_plug)
        state = controller.get_state()
        return jsonify(
            {
                "name": name,
                "state": state.value,
                "plug_type": sbc.power_plug.plug_type.value,
                "plug_address": sbc.power_plug.address,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route("/sbcs/<name>/power", methods=["POST"])
def control_power(name: str):
    """Control power for SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    if not sbc.power_plug:
        return jsonify({"error": "No power plug assigned"}), 400

    data = _json_body()
    action = data.get("action")
    if not isinstance(action, str):
        return jsonify({"error": "action is required (on, off, cycle)"}), 400

    action = action.lower()
    if action not in ["on", "off", "cycle"]:
        return jsonify({"error": "action must be on, off, or cycle"}), 400

    delay = None
    if action == "cycle":
        # Omitted: the outlet's minimum (runtime.board_cycle_delay: the
        # largest of the boards' stored delays, 3 s for one with none),
        # which is also the minimum of a given delay. An out-of-range
        # value, or an out-of-range stored one, is refused before anything
        # is switched.
        if "delay" in data and data["delay"] is None:
            return (
                jsonify({"error": "delay must be a number of seconds, not null"}),
                400,
            )
        try:
            delay = board_cycle_delay(g.manager, sbc, data.get("delay"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    refusal = claims.check(sbc.name)
    if refusal:
        return _refused(refusal)

    # Rate limit power cycles to prevent hardware damage. Marked before
    # the cycle starts: it blocks for the whole delay, and an overlapping
    # request must not start a second one.
    if action == "cycle":
        busy = begin_power_cycle(sbc)
        if busy:
            return jsonify({"error": busy}), 429
    elif outlet_cycle_in_progress(sbc):
        # Switching the outlet mid-cycle would undo or race the cycle.
        return (
            jsonify(
                {
                    "error": (
                        f"a power cycle of {sbc.name!r}'s power outlet is in "
                        "progress; retry when it has finished"
                    ),
                    "switched": False,
                }
            ),
            409,
        )

    success = False
    try:
        controller = PowerController.from_plug(sbc.power_plug)

        if action == "on":
            success = power_on_with_bindings(g.manager, sbc, controller)
        elif action == "off":
            success = controller.power_off()
        else:
            success = power_cycle_with_bindings(g.manager, sbc, controller, delay)

        if success:
            state = controller.get_state()
            body = {
                "name": name,
                "action": action,
                "success": True,
                "state": state.value,
            }
            if action == "cycle":
                body["delay_seconds"] = delay.seconds
                body["delay_source"] = delay.source
            return jsonify(body)
        else:
            return (
                jsonify(
                    {
                        "name": name,
                        "action": action,
                        "success": False,
                        "error": "Power operation failed",
                    }
                ),
                500,
            )
    except ActuationError as e:
        # Refused before anything was switched (the pre-check, or a strap
        # that failed before power-on): the board is as it was. A strap
        # failure after a cycle's power-off is a 500.
        if isinstance(e, PrePowerCheckError) or action == "on":
            return jsonify({"error": str(e), "switched": False}), 409
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if action == "cycle":
            end_power_cycle(sbc, success)


# --- Port Endpoints ---


@api_bp.route("/sbcs/<name>/ports", methods=["POST"])
def assign_port(name: str):
    """Assign serial port to SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    data = request.get_json()
    if not data or "type" not in data or "device" not in data:
        return jsonify({"error": "type and device are required"}), 400
    if not isinstance(data["device"], str) or not data["device"].strip():
        return jsonify({"error": "device must be a device path"}), 400

    try:
        port_type = PortType(data["type"])
    except ValueError:
        return jsonify({"error": f"Invalid port type: {data['type']}"}), 400

    # Resolve serial device name to ID
    serial_device_id = None
    sd_name = data.get("serial_device")
    if sd_name:
        sd = g.manager.get_serial_device_by_name(sd_name)
        if sd:
            serial_device_id = sd.id

    # The boards already wired to this console are checked too: otherwise
    # a claimed board's console could be typed into through this one.
    refusal = claims.check(
        sbc.name,
        shared=False,
        sharers=dict(
            serial_device_path=data["device"],
            serial_device_id=serial_device_id,
            # Anything else is left to assign_serial_port to refuse.
            tcp_port=claims.port_number(data.get("tcp_port")),
        ),
    )
    if refusal:
        return _refused(refusal)

    try:
        port = g.manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=port_type,
            device_path=data["device"],
            tcp_port=data.get("tcp_port"),
            baud_rate=data.get("baud_rate", 115200),
            alias=data.get("alias"),
            serial_device_id=serial_device_id,
            base_tcp_port=g.config.serial.base_tcp_port,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return (
        jsonify(
            {
                "id": port.id,
                "type": port.port_type.value,
                "device": port.device_path,
                "tcp_port": port.tcp_port,
                "baud_rate": port.baud_rate,
                "alias": port.alias,
            }
        ),
        201,
    )


@api_bp.route("/sbcs/<name>/console/info", methods=["GET"])
def get_console_info(name: str):
    """Get console connection info for an SBC.

    Returns the information needed to connect to the SBC's serial console,
    including TCP port, baud rate, and proxy port if available.
    """
    from flask import current_app

    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    console_port = sbc.console_port
    if not console_port:
        return jsonify({"error": "No console port assigned"}), 400

    config = current_app.config.get("LABCTL_CONFIG")

    response = {
        "name": name,
        "device_path": console_port.device_path,
        "baud_rate": console_port.baud_rate,
        "tcp_port": console_port.tcp_port,
        "tcp_host": "localhost",
    }

    # Add proxy info if available
    if config and config.proxy:
        response["proxy"] = {
            "port_base": config.proxy.port_base,
            "write_policy": config.proxy.write_policy,
            "max_clients": config.proxy.max_clients,
        }

    # Add connection commands
    if console_port.tcp_port:
        response["connect_commands"] = {
            "netcat": f"nc localhost {console_port.tcp_port}",
            "telnet": f"telnet localhost {console_port.tcp_port}",
        }
    else:
        baud = console_port.baud_rate
        dev = console_port.device_path
        response["connect_commands"] = {
            "picocom": f"picocom -b {baud} {dev}",
        }

    return jsonify(response)


@api_bp.route("/ports", methods=["GET"])
def list_ports():
    """List all serial port assignments."""
    ports = g.manager.list_serial_ports()

    # Get SBC names
    sbc_names = {}
    for sbc in g.manager.list_sbcs():
        sbc_names[sbc.id] = sbc.name

    return jsonify(
        {
            "ports": [
                {
                    "id": p.id,
                    "sbc_id": p.sbc_id,
                    "sbc_name": sbc_names.get(p.sbc_id),
                    "type": p.port_type.value,
                    "device": p.device_path,
                    "tcp_port": p.tcp_port,
                    "baud_rate": p.baud_rate,
                }
                for p in ports
            ],
            "count": len(ports),
        }
    )


# --- Status Endpoints ---


@api_bp.route("/health", methods=["GET"])
def health_check():
    """System health check."""
    return jsonify(
        {
            "status": "healthy",
            "version": __version__,
        }
    )


@api_bp.route("/status", methods=["GET"])
def get_status():
    """Get status overview of all SBCs."""
    sbcs = g.manager.list_sbcs()

    status_list = []
    for sbc in sbcs:
        status_data = {
            "name": sbc.name,
            "project": sbc.project,
            "status": sbc.status.value,
            "ip": sbc.primary_ip,
            "power": None,
        }

        # Get power state if plug assigned
        if sbc.power_plug:
            try:
                controller = PowerController.from_plug(sbc.power_plug)
                state = controller.get_state()
                status_data["power"] = state.value
            except Exception:
                status_data["power"] = "error"

        status_list.append(status_data)

    return jsonify(
        {
            "sbcs": status_list,
            "count": len(status_list),
        }
    )


@api_bp.route("/sbcs/<name>/history", methods=["GET"])
def get_sbc_history(name: str):
    """Get status history for an SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    limit = request.args.get("limit", 100, type=int)
    history = g.manager.get_status_history(sbc_id=sbc.id, limit=limit)

    return jsonify(
        {
            "sbc_name": name,
            "history": history,
            "count": len(history),
        }
    )


@api_bp.route("/sbcs/<name>/uptime", methods=["GET"])
def get_sbc_uptime(name: str):
    """Get uptime statistics for an SBC."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        return jsonify({"error": f"SBC '{name}' not found"}), 404

    uptime = g.manager.get_uptime(sbc.id)
    if uptime is None:
        return jsonify({"error": "No uptime data available"}), 404

    return jsonify(uptime)


@api_bp.route("/health/check", methods=["GET", "POST"])
def run_health_check():
    """Run health checks on SBCs.

    GET: Returns cached/last check results
    POST: Runs a new health check and returns results
    """
    from flask import current_app

    from labctl.health import CheckType, HealthChecker

    # Get config from app
    config = current_app.config.get("LABCTL_CONFIG")

    # Create checker
    checker = HealthChecker(
        ping_timeout=config.health.ping_timeout if config else 2.0,
        serial_timeout=config.health.serial_timeout if config else 2.0,
    )

    # Get optional SBC filter
    sbc_name = request.args.get("sbc")

    # Get check types
    check_type = request.args.get("type", "all")
    if check_type == "all":
        types = [CheckType.PING, CheckType.SERIAL, CheckType.POWER]
    else:
        try:
            types = [CheckType(check_type)]
        except ValueError:
            return jsonify({"error": f"Invalid check type: {check_type}"}), 400

    # Get SBCs to check
    if sbc_name:
        sbc = g.manager.get_sbc_by_name(sbc_name)
        if not sbc:
            return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
        sbcs = [sbc]
    else:
        sbcs = g.manager.list_sbcs()

    # Run checks
    results = checker.check_all(sbcs, types)

    # Format results for JSON
    output = {}
    for name, summary in results.items():
        sbc_result = {
            "recommended_status": (
                summary.recommended_status.value if summary.recommended_status else None
            ),
        }

        if summary.ping_result:
            sbc_result["ping"] = {
                "success": summary.ping_result.success,
                "message": summary.ping_result.message,
                "duration_ms": summary.ping_result.duration_ms,
            }

        if summary.serial_result:
            sbc_result["serial"] = {
                "success": summary.serial_result.success,
                "message": summary.serial_result.message,
                "duration_ms": summary.serial_result.duration_ms,
            }

        if summary.power_result:
            sbc_result["power"] = {
                "success": summary.power_result.success,
                "message": summary.power_result.message,
                "duration_ms": summary.power_result.duration_ms,
                "state": summary.power_state.value if summary.power_state else None,
            }

        output[name] = sbc_result

    return jsonify(
        {
            "results": output,
            "count": len(output),
        }
    )


# --- Claim Endpoints ---


@api_bp.route("/claims", methods=["GET"])
def list_claims():
    """List all active claims."""
    claims = g.manager.list_active_claims()
    return jsonify({"claims": [c.to_dict() for c in claims], "count": len(claims)})


@api_bp.route("/claims/<sbc_name>", methods=["GET"])
def get_claim(sbc_name: str):
    """Get the current claim on an SBC (or 'claimed: false')."""
    from labctl.core.models import UnknownSBCError

    try:
        claim = g.manager.get_active_claim(sbc_name)
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404

    if claim is None:
        return jsonify({"sbc_name": sbc_name, "claimed": False})
    return jsonify({"sbc_name": sbc_name, "claimed": True, "claim": claim.to_dict()})


@api_bp.route("/claims/<sbc_name>/history", methods=["GET"])
def get_claim_history(sbc_name: str):
    """Past claims for an SBC."""
    from labctl.core.models import UnknownSBCError

    limit = request.args.get("limit", 20, type=int)
    try:
        history = g.manager.list_claim_history(sbc_name, limit=limit)
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    return jsonify({"history": [c.to_dict() for c in history]})


def _duration_seconds(value):
    """Minutes from a request body as seconds within the configured bounds,
    or an error response."""
    claims_config = g.config.claims
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        value = None
    try:
        duration_s = int(value) * 60
    except (TypeError, ValueError):
        return None, (
            jsonify(
                {
                    "error": "invalid_duration",
                    "message": "duration_minutes must be a whole number",
                }
            ),
            400,
        )
    min_s = claims_config.min_duration_minutes * 60
    max_s = claims_config.max_duration_minutes * 60
    if duration_s < min_s or duration_s > max_s:
        return None, (
            jsonify(
                {
                    "error": "duration_out_of_bounds",
                    "message": (
                        f"Duration {duration_s // 60}m is outside "
                        f"[{claims_config.min_duration_minutes}m, "
                        f"{claims_config.max_duration_minutes}m]"
                    ),
                }
            ),
            400,
        )
    return duration_s, None


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@api_bp.route("/claims/<sbc_name>", methods=["POST"])
def create_claim(sbc_name: str):
    """Claim exclusive access to an SBC.

    The claim belongs to the caller's user (with auth), so their other
    sessions (the web UI, MCP over HTTP) count as holding it too.
    """
    from labctl.actuators.runtime import ChannelBusyError
    from labctl.core.models import ClaimConflict, UnknownSBCError

    data = _json_body()
    config = g.config
    duration_s, error = _duration_seconds(
        data.get("duration_minutes", config.claims.default_duration_minutes)
    )
    if error:
        return error
    reason = data.get("reason") or "web claim"
    agent_name = data.get("agent_name")
    if agent_name is None:
        agent_name = ""
    if not isinstance(reason, str) or not isinstance(agent_name, str):
        return jsonify({"error": "reason and agent_name must be strings"}), 400
    agent_name = agent_name.strip()
    if not agent_name:
        # Without auth there is no user to name the claim after.
        if config.claims.require_agent_name and not claims.web_actor().owner:
            return (
                jsonify(
                    {
                        "error": "agent_name_required",
                        "message": (
                            "This lab requires claims to name the agent "
                            "(claims.require_agent_name): send agent_name"
                        ),
                    }
                ),
                400,
            )
        agent_name = claims.web_user_name()

    manager = g.manager
    actor = claims.web_actor()
    grace = config.claims.grace_period_seconds

    def already_claimed(claim):
        return (
            jsonify(
                {
                    "error": "already_claimed",
                    "sbc_name": sbc_name,
                    "message": (
                        f"You already hold the claim on '{sbc_name}'; extend it "
                        f"with POST /api/claims/{sbc_name}/renew"
                    ),
                    "claim": claim.to_dict(),
                }
            ),
            409,
        )

    try:
        if config.claims.enabled:
            own = manager.get_open_claim(sbc_name)
            if own is not None and own.is_active and actor.holds(own):
                return already_claimed(own)
            # The board's own holder, or another user's claim on a board
            # sharing its hardware (the new claim would let the caller
            # drive that board).
            blocking = manager.new_claim_conflict(sbc_name, actor, grace)
            if blocking is not None:
                return (
                    jsonify(
                        claims.claimed_error(sbc_name, blocking, override_hint=False)
                    ),
                    409,
                )
        claim = manager.claim_sbc(
            sbc_name=sbc_name,
            agent_name=agent_name,
            session_id=actor.session_id,
            session_kind="web",
            duration_seconds=duration_s,
            reason=reason,
            grace_seconds=grace,
            owner=actor.owner,
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimConflict as exc:
        if actor.holds(exc.claim):
            return already_claimed(exc.claim)
        return (
            jsonify(claims.claimed_error(sbc_name, exc.claim, override_hint=False)),
            409,
        )
    except ChannelBusyError as exc:
        return jsonify({"error": "channel_busy", "message": str(exc)}), 409

    return jsonify({"status": "claimed", "claim": claim.to_dict()}), 201


@api_bp.route("/claims/<sbc_name>/release", methods=["POST"])
def release_claim(sbc_name: str):
    """Release a claim (the caller's user must hold it)."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    actor = claims.web_actor()
    try:
        g.manager.release_claim(
            sbc_name,
            actor.session_id,
            owner=actor.owner,
            released_by=claims.web_user_name(),
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    except NotClaimantError:
        return jsonify({"error": "not_claimant"}), 403
    return jsonify({"status": "released"})


@api_bp.route("/claims/<sbc_name>/renew", methods=["POST"])
def renew_claim(sbc_name: str):
    """Renew / extend a claim (the caller's user must hold it)."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    data = _json_body()
    duration_s = None
    if data.get("duration_minutes") is not None:
        duration_s, error = _duration_seconds(data["duration_minutes"])
        if error:
            return error
    actor = claims.web_actor()
    try:
        claim = g.manager.renew_claim(
            sbc_name, actor.session_id, duration_seconds=duration_s, owner=actor.owner
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    except NotClaimantError:
        return jsonify({"error": "not_claimant"}), 403
    return jsonify({"status": "renewed", "claim": claim.to_dict()})


@api_bp.route("/claims/<sbc_name>/force-release", methods=["POST"])
def force_release_claim(sbc_name: str):
    """Operator override — forcibly release a claim (operators only)."""
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    if not claims.web_actor().operator:
        return (
            jsonify(
                {
                    "error": "not_operator",
                    "sbc_name": sbc_name,
                    "message": (
                        "Only operators may force-release a claim; your user "
                        "has role 'agent'."
                    ),
                    "hints": [
                        f"Ask for it: POST /api/claims/{sbc_name}/request-release"
                    ],
                }
            ),
            403,
        )
    data = _json_body()
    reason = data.get("reason") or "operator force-release via web"
    try:
        released = g.manager.force_release_claim(
            sbc_name, str(reason), released_by=claims.web_user_name()
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    return jsonify({"status": "force_released", "was_held_by": released.agent_name})


@api_bp.route("/claims/<sbc_name>/request-release", methods=["POST"])
def request_release(sbc_name: str):
    """Politely ask the claimant to release."""
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    data = _json_body()
    reason = data.get("reason") or "web request"
    try:
        g.manager.record_release_request(
            sbc_name, requested_by=claims.web_user_name(), reason=str(reason)
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    return jsonify({"status": "request_recorded"})


# --- Activity Stream Endpoints ---


@api_bp.route("/activity", methods=["GET"])
def list_activity():
    """List recent activity events, newest first.

    Query params:
      limit=N       Max events (default 50, max 1000).
      sbc=X         Filter by entity_name.
      actor=X       Filter by actor (exact match).
      source=X      cli | mcp | api | web | daemon | internal.
      result=X      ok | error | forbidden.
      since=TS      ISO timestamp lower bound.
      after_id=N    Only events with id > N (for long polling).
    """
    try:
        limit = min(int(request.args.get("limit", 50)), 1000)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    since = request.args.get("since")
    after_id = request.args.get("after_id")
    if after_id:
        try:
            after_id_int = int(after_id)
        except ValueError:
            return jsonify({"error": "after_id must be an integer"}), 400
    else:
        after_id_int = None

    events = audit.query_events(
        g.manager.db,
        limit=limit,
        sbc=request.args.get("sbc"),
        actor=request.args.get("actor"),
        source=request.args.get("source"),
        result=request.args.get("result"),
        since=since,
        after_id=after_id_int,
    )
    return jsonify({"events": events, "count": len(events)})
