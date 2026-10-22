"""
Shared pytest fixtures for labctl tests.
"""

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _restore_labctl_config_env(monkeypatch):
    """`labctl -c FILE mcp` writes LABCTL_CONFIG / _EXCLUSIVE straight into
    os.environ, and the MCP server's run_server() snapshots its config in
    labctl.core.config. Reset both around every test; otherwise one test
    would decide every later load_config() (a deleted temp file, or a
    stale snapshot)."""
    for name in ("LABCTL_CONFIG", "LABCTL_CONFIG_EXCLUSIVE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    from labctl.core import config as config_mod

    monkeypatch.setattr(config_mod, "_snapshot", None)


@pytest.fixture(autouse=True)
def _reset_mcp_upload_security():
    """run_server() also records the HTTP Host/Origin settings for the
    upload route (labctl.mcp_server._http_security); reset it after every
    test, without importing the optional MCP SDK where it isn't used."""
    yield
    server = sys.modules.get("labctl.mcp_server")
    if server is not None:
        server._http_security = server._UNSET
        server._http_tls = False


@pytest.fixture
def sdwire_stub(monkeypatch):
    """Install a hardware-free stand-in for the optional ``sdwire`` package.

    The real package is an optional extra that requires Python >= 3.12 and
    talks to USB. Tests that exercise ``labctl.sdwire`` use this stub so they
    behave identically on 3.10-3.13 whether or not ``sdwire`` is installed,
    and never touch real hardware. Detection returns no devices unless a test
    patches ``sdwire.backend.detect.get_sdwire(c)_devices``.
    """
    detect = types.ModuleType("sdwire.backend.detect")
    detect.get_sdwire_devices = lambda: []
    detect.get_sdwirec_devices = lambda: []

    backend = types.ModuleType("sdwire.backend")
    backend.detect = detect

    root = types.ModuleType("sdwire")
    root.backend = backend

    monkeypatch.setitem(sys.modules, "sdwire", root)
    monkeypatch.setitem(sys.modules, "sdwire.backend", backend)
    monkeypatch.setitem(sys.modules, "sdwire.backend.detect", detect)
    return detect
