"""
Shared pytest fixtures for labctl tests.
"""

import sys
import types

import pytest


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
