"""
Web interface for lab controller.

Provides REST API and web dashboard for managing lab resources.
"""

from labctl.web.app import create_app

__all__ = ["create_app"]
