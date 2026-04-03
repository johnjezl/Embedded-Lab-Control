"""
Authentication module for lab controller web interface.

Provides session-based login for web UI and API key auth for REST API.
"""

import hmac
import secrets
from typing import Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from labctl.core.config import AuthConfig, UserConfig

auth_bp = Blueprint("auth", __name__)


def get_user_by_username(
    auth_config: AuthConfig, username: str
) -> Optional[UserConfig]:
    """Look up a user by username."""
    for user in auth_config.users:
        if user.username == username:
            return user
    return None


def get_user_by_api_key(
    auth_config: AuthConfig, api_key: str
) -> Optional[UserConfig]:
    """Look up a user by API key using constant-time comparison."""
    for user in auth_config.users:
        if user.api_key and hmac.compare_digest(user.api_key, api_key):
            return user
    return None


def verify_password(user: UserConfig, password: str) -> bool:
    """Verify a password against a user's stored hash."""
    if not user.password_hash:
        return False
    return check_password_hash(user.password_hash, password)


def generate_csrf_token() -> str:
    """Generate or retrieve a CSRF token for the current session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def validate_csrf_token() -> bool:
    """Validate CSRF token from form data against session."""
    token = request.form.get("_csrf_token", "")
    session_token = session.get("_csrf_token", "")
    if not token or not session_token:
        return False
    return hmac.compare_digest(token, session_token)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Handle login page and form submission."""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        config = current_app.config["LABCTL_CONFIG"]
        user = get_user_by_username(config.auth, username)

        if user and verify_password(user, password):
            session["user"] = username
            session.permanent = True
            flash("Logged in successfully.", "success")
            next_url = request.args.get("next", "")
            # Prevent open redirect: only allow relative paths
            if not next_url or next_url.startswith("//") or "://" in next_url:
                next_url = url_for("views.index")
            return redirect(next_url)
        else:
            flash("Invalid username or password.", "error")

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Log out the current user."""
    session.pop("user", None)
    session.pop("_csrf_token", None)
    flash("Logged out.", "success")
    return redirect(url_for("auth.login"))
