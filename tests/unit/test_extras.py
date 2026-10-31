"""Tests for the install-the-extra messages on a bare install.

A None entry in sys.modules makes importing that module raise
ModuleNotFoundError, which is how a missing optional package looks.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from labctl import extras
from labctl.cli import main
from labctl.extras import EXTRAS, MissingExtraError, is_missing, missing_extra_message

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


class _Version(tuple):
    """Stand-in for sys.version_info (comparable, with major/minor)."""

    @property
    def major(self):
        return self[0]

    @property
    def minor(self):
        return self[1]


class TestMessages:
    @pytest.mark.parametrize("extra", sorted(EXTRAS))
    def test_names_the_extra_on_one_line(self, extra):
        message = missing_extra_message(extra)
        assert f'pip install "embedded-lab-control[{extra}]"' in message
        assert "pipx inject embedded-lab-control" in message
        assert "\n" not in message

    def test_pipx_quotes_version_specifiers(self):
        """`mcp>=2.2,<3` unquoted would be a shell redirection."""
        assert '"mcp>=2.2,<3"' in missing_extra_message("mcp")

    def test_sdwire_mentions_python_312(self, monkeypatch):
        monkeypatch.setattr(extras, "sys", type("S", (), {}))
        extras.sys.version_info = _Version((3, 12, 1))
        assert missing_extra_message("sdwire").endswith("; sdwire needs Python 3.12+")

    def test_sdwire_on_old_python_says_extra_installs_nothing(self, monkeypatch):
        monkeypatch.setattr(extras, "sys", type("S", (), {}))
        extras.sys.version_info = _Version((3, 10, 14))
        message = missing_extra_message("sdwire")
        assert "needs Python 3.12+, and this is Python 3.10" in message
        assert "installs nothing" in message

    def test_error_is_runtime_error_with_extra(self):
        e = MissingExtraError("kasa")
        assert isinstance(e, RuntimeError)
        assert e.extra == "kasa"
        assert str(e) == missing_extra_message("kasa")

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
    def test_extras_match_pyproject(self):
        """Every user-facing extra in pyproject has a message, and no
        message names an extra that doesn't exist."""
        import tomllib

        if not PYPROJECT.exists():
            pytest.skip("pyproject.toml not available")
        data = tomllib.loads(PYPROJECT.read_text())
        declared = set(data["project"]["optional-dependencies"]) - {"dev"}
        assert set(EXTRAS) == declared


class TestIsMissing:
    def test_matches_module_and_submodules(self):
        assert is_missing(ModuleNotFoundError(name="flask"), "flask")
        assert is_missing(ModuleNotFoundError(name="mcp.server"), "mcp")

    def test_other_modules_do_not_match(self):
        assert not is_missing(ModuleNotFoundError(name="mcpx"), "mcp")
        assert not is_missing(ModuleNotFoundError(name="werkzeug"), "flask")
        assert not is_missing(ModuleNotFoundError(), "flask")


class TestWebCommand:
    def _invoke_without(self, absent):
        # Never serve for real if the import unexpectedly succeeds.
        with patch("flask.Flask.run") as run, patch.dict("sys.modules"):
            # Drop labctl.web so the command re-imports it against the gap;
            # patch.dict puts the originals back afterwards.
            for name in list(sys.modules):
                if name == "labctl.web" or name.startswith("labctl.web."):
                    del sys.modules[name]
            sys.modules.update({m: None for m in absent})
            result = CliRunner().invoke(main, ["web"])
        run.assert_not_called()
        return result

    def test_missing_flask_gives_install_hint(self):
        result = self._invoke_without(["flask"])
        assert result.exit_code == 1
        assert 'pip install "embedded-lab-control[web]"' in result.output
        assert "Traceback" not in result.output

    def test_other_missing_module_keeps_its_error(self):
        """Only the extra's packages are translated; anything else is a
        bug and must not be disguised as a missing extra."""
        result = self._invoke_without(["labctl.web.api"])
        assert isinstance(result.exception, ModuleNotFoundError)
        assert "embedded-lab-control[web]" not in result.output


class TestSdwireCommand:
    def test_discover_without_sdwire(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        absent = {"sdwire": None, "sdwire.backend": None, "sdwire.backend.detect": None}
        with patch.dict("sys.modules", absent):
            result = CliRunner().invoke(main, ["-c", str(config), "sdwire", "discover"])
        assert result.exit_code == 1
        assert 'pip install "embedded-lab-control[sdwire]"' in result.output
        assert "Python 3.12+" in result.output
        assert "Traceback" not in result.output


class TestKasa:
    def test_power_on_without_python_kasa(self):
        from labctl.power import kasa as kasa_mod

        kasa_mod._get_cached_kasa_credentials.cache_clear()
        controller = kasa_mod.KasaController("192.0.2.77")
        absent = {"kasa": None, "kasa.device_factory": None, "kasa.iot": None}
        try:
            with patch.dict("sys.modules", absent):
                with pytest.raises(MissingExtraError) as info:
                    controller.power_on()
        finally:
            kasa_mod._get_cached_kasa_credentials.cache_clear()
        assert info.value.extra == "kasa"
        assert "embedded-lab-control[kasa]" in str(info.value)


class TestActuators:
    def test_open_without_pyserial(self):
        from labctl.actuators import DriverError, Transport
        from labctl.actuators.lcus1 import Lcus1SerialDriver

        driver = Lcus1SerialDriver(expected_channel_count=1)
        with patch.dict("sys.modules", {"serial": None}):
            with pytest.raises(DriverError, match=r"embedded-lab-control\[actuators\]"):
                driver.open(Transport(device_path="/nonexistent/relay"))

    def test_probe_without_pyserial(self):
        from labctl.actuators import ProbeResult, Transport
        from labctl.actuators.lcus1 import Lcus1SerialDriver

        driver = Lcus1SerialDriver(expected_channel_count=1)
        # probe() opens transiently; it needs the transport hint from open().
        driver._transport = Transport(device_path="/nonexistent/relay")
        with patch.dict("sys.modules", {"serial": None}):
            outcome = driver.probe()
        assert outcome.result is ProbeResult.UNKNOWN
        assert "embedded-lab-control[actuators]" in outcome.detail
