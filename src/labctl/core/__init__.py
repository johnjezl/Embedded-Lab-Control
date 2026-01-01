"""
Core components for lab controller.

Provides configuration, database, and resource management.
"""

from labctl.core.config import Config, load_config

__all__ = ["Config", "load_config"]
