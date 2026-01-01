"""
REST API endpoints for lab controller.
"""

from flask import Blueprint, jsonify, request, g

from labctl.core.models import Status, PortType, AddressType, PlugType
from labctl.power import PowerController, PowerState

api_bp = Blueprint("api", __name__)


def sbc_to_dict(sbc) -> dict:
    """Convert SBC model to JSON-serializable dict."""
    data = {
        "id": sbc.id,
        "name": sbc.name,
        "project": sbc.project,
        "description": sbc.description,
        "ssh_user": sbc.ssh_user,
        "status": sbc.status.value,
        "primary_ip": sbc.primary_ip,
    }

    # Add serial ports
    if sbc.serial_ports:
        data["serial_ports"] = [
            {
                "id": p.id,
                "type": p.port_type.value,
                "device": p.device_path,
                "tcp_port": p.tcp_port,
                "baud_rate": p.baud_rate,
            }
            for p in sbc.serial_ports
        ]

    # Add network addresses
    if sbc.network_addresses:
        data["network_addresses"] = [
            {
                "id": a.id,
                "type": a.address_type.value,
                "ip": a.ip_address,
                "mac": a.mac_address,
                "hostname": a.hostname,
            }
            for a in sbc.network_addresses
        ]

    # Add power plug
    if sbc.power_plug:
        data["power_plug"] = {
            "id": sbc.power_plug.id,
            "type": sbc.power_plug.plug_type.value,
            "address": sbc.power_plug.address,
            "index": sbc.power_plug.plug_index,
        }

    return data


# --- SBC Endpoints ---

@api_bp.route("/sbcs", methods=["GET"])
def list_sbcs():
    """List all SBCs."""
    project = request.args.get("project")
    status = request.args.get("status")

    status_filter = Status(status) if status else None
    sbcs = g.manager.list_sbcs(project=project, status=status_filter)

    return jsonify({
        "sbcs": [sbc_to_dict(sbc) for sbc in sbcs],
        "count": len(sbcs),
    })


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
        return jsonify({
            "name": name,
            "state": state.value,
            "plug_type": sbc.power_plug.plug_type.value,
            "plug_address": sbc.power_plug.address,
        })
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

    try:
        controller = PowerController.from_plug(sbc.power_plug)

        if action == "on":
            success = controller.power_on()
        elif action == "off":
            success = controller.power_off()
        else:
            delay = data.get("delay", 2.0)
            success = controller.power_cycle(delay)

        if success:
            state = controller.get_state()
            return jsonify({
                "name": name,
                "action": action,
                "success": True,
                "state": state.value,
            })
        else:
            return jsonify({
                "name": name,
                "action": action,
                "success": False,
                "error": "Power operation failed",
            }), 500
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

    port = g.manager.assign_serial_port(
        sbc_id=sbc.id,
        port_type=port_type,
        device_path=data["device"],
        tcp_port=data.get("tcp_port"),
        baud_rate=data.get("baud_rate", 115200),
    )

    return jsonify({
        "id": port.id,
        "type": port.port_type.value,
        "device": port.device_path,
        "tcp_port": port.tcp_port,
        "baud_rate": port.baud_rate,
    }), 201


@api_bp.route("/ports", methods=["GET"])
def list_ports():
    """List all serial port assignments."""
    ports = g.manager.list_serial_ports()

    # Get SBC names
    sbc_names = {}
    for sbc in g.manager.list_sbcs():
        sbc_names[sbc.id] = sbc.name

    return jsonify({
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
    })


# --- Status Endpoints ---

@api_bp.route("/health", methods=["GET"])
def health_check():
    """System health check."""
    return jsonify({
        "status": "healthy",
        "version": "0.1.0",
    })


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

    return jsonify({
        "sbcs": status_list,
        "count": len(status_list),
    })
