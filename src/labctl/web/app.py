"""
Flask application factory for lab controller web interface.
"""

import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, session, url_for

from labctl.core import audit
from labctl.core.config import Config, load_config
from labctl.core.manager import ResourceManager, get_manager
from labctl.web.auth import (
    generate_csrf_token,
    get_user_by_api_key,
    validate_csrf_token,
)


def create_app(config: Config | None = None) -> Flask:
    """
    Create and configure Flask application.

    Args:
        config: Optional Config instance. If None, loads from default location.

    Returns:
        Configured Flask application
    """
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    # Load configuration
    if config is None:
        config = load_config()

    app.config["LABCTL_CONFIG"] = config

    # Set SECRET_KEY from auth config, fall back to random if empty
    if config.auth.secret_key:
        app.config["SECRET_KEY"] = config.auth.secret_key
    elif config.auth.enabled:
        app.config["SECRET_KEY"] = secrets.token_hex(32)
    else:
        app.config["SECRET_KEY"] = "labctl-dev-key"

    # Set session lifetime
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=config.auth.session_lifetime_minutes
    )

    # Register blueprints
    from labctl.web.activity_broadcaster import ActivityBroadcaster
    from labctl.web.api import api_bp
    from labctl.web.auth import auth_bp
    from labctl.web.views import views_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(views_bp)

    # Start the activity-stream broadcaster thread. Polls audit_log and
    # fans new rows out to SSE subscribers on /activity/stream.
    mgr_for_broadcast = get_manager(config.database_path)
    app.config["ACTIVITY_BROADCASTER"] = ActivityBroadcaster(mgr_for_broadcast.db)
    app.config["ACTIVITY_BROADCASTER"].start()

    # Register csrf_token as Jinja2 template global
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    def _request_activity_identity() -> tuple[str, str]:
        """Derive audit actor/source for the current Flask request."""
        source = (
            "api" if request.endpoint and request.endpoint.startswith("api.") else "web"
        )

        user = getattr(g, "audit_user", None)
        if not user:
            user = session.get("user")
        if not user:
            user = "anonymous"

        return f"{source}:{user}", source

    @app.before_request
    def before_request():
        """Set up manager and enforce auth for each request."""
        config = app.config["LABCTL_CONFIG"]
        g.manager = get_manager(config.database_path)
        g.config = config

        api_request = bool(request.endpoint and request.endpoint.startswith("api."))
        api_user = None

        if api_request and config.auth.enabled:
            api_key = request.headers.get("X-API-Key", "")
            if api_key:
                api_user = get_user_by_api_key(config.auth, api_key)
                if api_user:
                    g.audit_user = api_user.username
        elif "user" in session:
            g.audit_user = session["user"]

        actor, source = _request_activity_identity()
        ctx = audit.activity_context(actor, source)
        ctx.__enter__()
        g._activity_context = ctx

        # Skip auth enforcement if auth is disabled
        if not config.auth.enabled:
            return

        # Whitelist: login page, static files, health check
        if request.endpoint in (
            "auth.login",
            "auth.logout",
            "static",
            "api.health_check",
        ):
            return

        # API requests: check X-API-Key header
        if api_request:
            api_key = request.headers.get("X-API-Key", "")
            if not api_key:
                return jsonify({"error": "API key required"}), 401
            if not api_user:
                return jsonify({"error": "Invalid API key"}), 401

        # Web requests: check session
        if "user" not in session:
            if not api_request:
                if request.endpoint not in ("auth.login", "auth.logout", "static"):
                    return redirect(url_for("auth.login", next=request.path))

    @app.teardown_request
    def teardown_request(exc):
        """Reset per-request audit attribution context."""
        ctx = getattr(g, "_activity_context", None)
        if ctx is not None:
            ctx.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)

    @app.before_request
    def enforce_csrf():
        """Enforce CSRF token on state-changing web requests."""
        config = app.config["LABCTL_CONFIG"]
        if not config.auth.enabled:
            return

        # Only enforce on state-changing methods
        if request.method not in ("POST", "PUT", "DELETE"):
            return

        # Skip for API endpoints (they use API keys, not CSRF)
        if request.endpoint and request.endpoint.startswith("api."):
            return

        # Skip for login (uses its own CSRF)
        if request.endpoint == "auth.login":
            return

        if not validate_csrf_token():
            return redirect(request.referrer or url_for("views.index"))

    return app


def get_manager_from_app() -> ResourceManager:
    """Get resource manager from Flask g object."""
    return g.manager
