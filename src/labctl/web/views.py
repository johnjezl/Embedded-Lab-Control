"""
Web views for lab controller dashboard.
"""

from flask import Blueprint, render_template, g, redirect, url_for, request, flash

from labctl.core.models import Status, PortType, AddressType, PlugType
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
