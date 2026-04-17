"""
REST API endpoints for lab controller.
"""

import time

from flask import Blueprint, g, jsonify, request, session

from labctl.core.models import PortType, Status
from labctl.power import PowerController

api_bp = Blueprint("api", __name__)

# Rate limiting: track last power cycle time per SBC to prevent hardware damage
_power_cycle_times: dict[str, float] = {}
POWER_CYCLE_MIN_INTERVAL = 5.0  # seconds


def sbc_to_dict(sbc) -> dict:
    """Convert SBC model to JSON-serializable dict."""
    return sbc.to_dict(include_ids=True)


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

    if g.manager.delete_sbc(sbc.id):
        return jsonify({"message": f"SBC '{name}' deleted"}), 200
    else:
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

    data = request.get_json()
    if not data or "action" not in data:
        return jsonify({"error": "action is required (on, off, cycle)"}), 400

    action = data["action"].lower()
    if action not in ["on", "off", "cycle"]:
        return jsonify({"error": "action must be on, off, or cycle"}), 400

    # Rate limit power cycles to prevent hardware damage
    if action == "cycle":
        last_cycle = _power_cycle_times.get(name, 0)
        elapsed = time.monotonic() - last_cycle
        if elapsed < POWER_CYCLE_MIN_INTERVAL:
            wait = POWER_CYCLE_MIN_INTERVAL - elapsed
            return (
                jsonify(
                    {
                        "error": f"Rate limited: wait {wait:.1f}s before next power cycle",
                    }
                ),
                429,
            )

    try:
        controller = PowerController.from_plug(sbc.power_plug)

        if action == "on":
            success = controller.power_on()
        elif action == "off":
            success = controller.power_off()
        else:
            delay = data.get("delay", 3.0)
            success = controller.power_cycle(delay)
            if success:
                _power_cycle_times[name] = time.monotonic()

        if success:
            state = controller.get_state()
            return jsonify(
                {
                    "name": name,
                    "action": action,
                    "success": True,
                    "state": state.value,
                }
            )
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
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

    try:
        port = g.manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=port_type,
            device_path=data["device"],
            tcp_port=data.get("tcp_port"),
            baud_rate=data.get("baud_rate", 115200),
            alias=data.get("alias"),
            serial_device_id=serial_device_id,
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
            "version": "0.1.0",
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


def _web_session_id() -> str:
    """Derive a stable session ID for web callers."""
    sid = session.get("_id") or session.get("session_id")
    if sid:
        return f"web-{sid}"
    # Fallback for API-key callers without a Flask session
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        return f"web-apikey-{api_key[:12]}"
    return "web-anonymous"


def _web_agent_name() -> str:
    """Agent name for web-originated claims."""
    return session.get("username", "web-operator")


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


@api_bp.route("/claims/<sbc_name>", methods=["POST"])
def create_claim(sbc_name: str):
    """Claim exclusive access to an SBC."""
    from flask import current_app

    from labctl.core.models import ClaimConflict, UnknownSBCError

    data = request.get_json(silent=True) or {}
    config = current_app.config.get("LABCTL_CONFIG")
    duration_min = data.get(
        "duration_minutes",
        config.claims.default_duration_minutes if config else 30,
    )
    reason = data.get("reason", "")
    agent_name = data.get("agent_name") or _web_agent_name()

    duration_s = int(duration_min) * 60
    if config:
        min_s = config.claims.min_duration_minutes * 60
        max_s = config.claims.max_duration_minutes * 60
        if duration_s < min_s or duration_s > max_s:
            return jsonify({"error": "duration_out_of_bounds"}), 400

    grace = config.claims.grace_period_seconds if config else 60
    try:
        claim = g.manager.claim_sbc(
            sbc_name=sbc_name,
            agent_name=agent_name,
            session_id=_web_session_id(),
            session_kind="web",
            duration_seconds=duration_s,
            reason=reason or "web claim",
            grace_seconds=grace,
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimConflict as exc:
        return jsonify({"error": "sbc_claimed", "claim": exc.claim.to_dict()}), 409

    return jsonify({"status": "claimed", "claim": claim.to_dict()}), 201


@api_bp.route("/claims/<sbc_name>/release", methods=["POST"])
def release_claim(sbc_name: str):
    """Release a claim (caller must be claimant)."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    try:
        g.manager.release_claim(sbc_name, _web_session_id())
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    except NotClaimantError:
        return jsonify({"error": "not_claimant"}), 403
    return jsonify({"status": "released"})


@api_bp.route("/claims/<sbc_name>/renew", methods=["POST"])
def renew_claim(sbc_name: str):
    """Renew / extend a claim."""
    from labctl.core.models import (
        ClaimNotFoundError,
        NotClaimantError,
        UnknownSBCError,
    )

    data = request.get_json(silent=True) or {}
    duration_min = data.get("duration_minutes")
    duration_s = int(duration_min) * 60 if duration_min is not None else None
    try:
        claim = g.manager.renew_claim(
            sbc_name, _web_session_id(), duration_seconds=duration_s
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
    """Operator override — forcibly release a claim."""
    from labctl.core.models import ClaimNotFoundError, UnknownSBCError

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "operator force-release via web")
    try:
        released = g.manager.force_release_claim(
            sbc_name, reason, released_by=_web_agent_name()
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

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")
    try:
        g.manager.record_release_request(
            sbc_name, requested_by=_web_agent_name(), reason=reason or "web request"
        )
    except UnknownSBCError:
        return jsonify({"error": f"SBC '{sbc_name}' not found"}), 404
    except ClaimNotFoundError:
        return jsonify({"error": "claim_not_found"}), 404
    return jsonify({"status": "request_recorded"})
