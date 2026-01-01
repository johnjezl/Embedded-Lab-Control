"""
Web views for lab controller dashboard.
"""

from flask import Blueprint, flash, g, redirect, render_template, url_for

from labctl.core.models import AddressType, PlugType, PortType, Status
from labctl.power import PowerController, PowerState

views_bp = Blueprint("views", __name__)


@views_bp.route("/")
def index():
    """Dashboard home page."""
    sbcs = g.manager.list_sbcs()

    # Get power states for SBCs with plugs
    power_states = {}
    for sbc in sbcs:
        if sbc.power_plug:
            try:
                controller = PowerController.from_plug(sbc.power_plug)
                power_states[sbc.name] = controller.get_state()
            except Exception:
                power_states[sbc.name] = PowerState.UNKNOWN

    return render_template(
        "dashboard.html",
        sbcs=sbcs,
        power_states=power_states,
        PowerState=PowerState,
    )


@views_bp.route("/sbc/<name>")
def sbc_detail(name: str):
    """SBC detail page."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    power_state = None
    if sbc.power_plug:
        try:
            controller = PowerController.from_plug(sbc.power_plug)
            power_state = controller.get_state()
        except Exception:
            power_state = PowerState.UNKNOWN

    return render_template(
        "sbc_detail.html",
        sbc=sbc,
        power_state=power_state,
        PowerState=PowerState,
        Status=Status,
        PortType=PortType,
        AddressType=AddressType,
        PlugType=PlugType,
    )


@views_bp.route("/sbc/<name>/power/<action>", methods=["POST"])
def sbc_power_action(name: str, action: str):
    """Handle power control actions."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    if not sbc.power_plug:
        flash("No power plug assigned", "error")
        return redirect(url_for("views.sbc_detail", name=name))

    try:
        controller = PowerController.from_plug(sbc.power_plug)

        if action == "on":
            success = controller.power_on()
            msg = "Power ON" if success else "Failed to power on"
        elif action == "off":
            success = controller.power_off()
            msg = "Power OFF" if success else "Failed to power off"
        elif action == "cycle":
            success = controller.power_cycle()
            msg = "Power cycled" if success else "Failed to power cycle"
        else:
            flash(f"Unknown action: {action}", "error")
            return redirect(url_for("views.sbc_detail", name=name))

        flash(msg, "success" if success else "error")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/console")
def sbc_console(name: str):
    """SBC console page with xterm.js terminal."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    # Find console port
    port = sbc.console_port

    return render_template(
        "console.html",
        sbc=sbc,
        port=port,
    )


@views_bp.route("/sbc/<name>/history")
def sbc_history(name: str):
    """SBC status history page."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    # Get status history
    history = g.manager.get_status_history(sbc_id=sbc.id, limit=100)

    return render_template(
        "status_history.html",
        sbc=sbc,
        history=history,
        Status=Status,
    )


@views_bp.route("/sbc/<name>/edit", methods=["POST"])
def sbc_edit(name: str):
    """Handle SBC edit form."""
    from flask import request

    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    project = request.form.get("project", "").strip() or None
    description = request.form.get("description", "").strip() or None
    ssh_user = request.form.get("ssh_user", "").strip() or "root"
    status = request.form.get("status", "").strip()

    status_enum = Status(status) if status else None

    g.manager.update_sbc(
        sbc.id,
        project=project,
        description=description,
        ssh_user=ssh_user,
        status=status_enum,
    )

    flash(f"Updated SBC '{name}'", "success")
    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/port/assign", methods=["POST"])
def sbc_port_assign(name: str):
    """Handle port assignment form."""
    from flask import request

    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    port_type = request.form.get("port_type", "").strip()
    device = request.form.get("device", "").strip()
    tcp_port = request.form.get("tcp_port", "").strip()
    baud_rate = request.form.get("baud_rate", "115200").strip()

    if not port_type or not device:
        flash("Port type and device are required", "error")
        return redirect(url_for("views.sbc_detail", name=name))

    try:
        g.manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType(port_type),
            device_path=device,
            tcp_port=int(tcp_port) if tcp_port else None,
            baud_rate=int(baud_rate),
        )
        flash(f"Assigned {port_type} port", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/port/remove/<port_type>", methods=["POST"])
def sbc_port_remove(name: str, port_type: str):
    """Remove port assignment."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    if g.manager.remove_serial_port(sbc.id, PortType(port_type)):
        flash(f"Removed {port_type} port", "success")
    else:
        flash(f"No {port_type} port to remove", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/network/set", methods=["POST"])
def sbc_network_set(name: str):
    """Handle network address form."""
    from flask import request

    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    address_type = request.form.get("address_type", "").strip()
    ip_address = request.form.get("ip_address", "").strip()
    mac_address = request.form.get("mac_address", "").strip() or None
    hostname = request.form.get("hostname", "").strip() or None

    if not address_type or not ip_address:
        flash("Address type and IP are required", "error")
        return redirect(url_for("views.sbc_detail", name=name))

    try:
        g.manager.set_network_address(
            sbc_id=sbc.id,
            address_type=AddressType(address_type),
            ip_address=ip_address,
            mac_address=mac_address,
            hostname=hostname,
        )
        flash(f"Set {address_type} address", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/network/remove/<address_type>", methods=["POST"])
def sbc_network_remove(name: str, address_type: str):
    """Remove network address."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    if g.manager.remove_network_address(sbc.id, AddressType(address_type)):
        flash(f"Removed {address_type} address", "success")
    else:
        flash(f"No {address_type} address to remove", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/plug/assign", methods=["POST"])
def sbc_plug_assign(name: str):
    """Handle power plug assignment form."""
    from flask import request

    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    plug_type = request.form.get("plug_type", "").strip()
    address = request.form.get("address", "").strip()
    plug_index = request.form.get("plug_index", "1").strip()

    if not plug_type or not address:
        flash("Plug type and address are required", "error")
        return redirect(url_for("views.sbc_detail", name=name))

    try:
        g.manager.assign_power_plug(
            sbc_id=sbc.id,
            plug_type=PlugType(plug_type),
            address=address,
            plug_index=int(plug_index),
        )
        flash(f"Assigned {plug_type} plug", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/sbc/<name>/plug/remove", methods=["POST"])
def sbc_plug_remove(name: str):
    """Remove power plug assignment."""
    sbc = g.manager.get_sbc_by_name(name)
    if not sbc:
        flash(f"SBC '{name}' not found", "error")
        return redirect(url_for("views.index"))

    if g.manager.remove_power_plug(sbc.id):
        flash("Removed power plug", "success")
    else:
        flash("No power plug to remove", "error")

    return redirect(url_for("views.sbc_detail", name=name))


@views_bp.route("/settings")
def settings():
    """Settings page."""
    from flask import current_app

    config = current_app.config.get("LABCTL_CONFIG")

    return render_template(
        "settings.html",
        config=config,
    )
