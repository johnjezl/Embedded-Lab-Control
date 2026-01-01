"""
Flask application factory for lab controller web interface.
"""

from pathlib import Path

from flask import Flask, g

from labctl.core.config import Config, load_config
from labctl.core.manager import ResourceManager, get_manager


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
    app.config["SECRET_KEY"] = "labctl-dev-key"  # Change in production

    # Register blueprints
    from labctl.web.api import api_bp
    from labctl.web.views import views_bp

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(views_bp)

    @app.before_request
    def before_request():
        """Set up manager for each request."""
        config = app.config["LABCTL_CONFIG"]
        g.manager = get_manager(config.database_path)
        g.config = config

    return app


def get_manager_from_app() -> ResourceManager:
    """Get resource manager from Flask g object."""
    return g.manager
