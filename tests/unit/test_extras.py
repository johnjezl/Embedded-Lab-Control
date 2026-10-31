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


def _fake_python(monkeypatch, *version):
    """Make labctl.extras see another interpreter version."""
    monkeypatch.setattr(extras, "sys", type("S", (), {}))
    extras.sys.version_info = _Version(version)


def _pyproject_extras():
    if sys.version_info < (3, 11):
        pytest.skip("tomllib is 3.11+")
    import tomllib

    if not PYPROJECT.exists():
        pytest.skip("pyproject.toml not available")
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["optional-dependencies"]


def _declared(extra, python_version):
    """pyproject's requirements for ``extra`` on that Python, as
    (name, specifier set) pairs."""
    from packaging.requirements import Requirement

    env = {"python_version": python_version, "extra": extra}
    reqs = [Requirement(r) for r in _pyproject_extras()[extra]]
    return {
        (r.name, r.specifier)
        for r in reqs
        if r.marker is None or r.marker.evaluate(env)
    }


def _offered(extra):
    from packaging.requirements import Requirement

    return {
        (r.name, r.specifier) for r in map(Requirement, extras.extra_packages(extra))
    }


class TestMessages:
    @pytest.mark.parametrize("extra", sorted(EXTRAS))
    def test_names_the_extra_on_one_line(self, extra, monkeypatch):
        _fake_python(monkeypatch, 3, 12, 0)
        message = missing_extra_message(extra)
        assert f'pip install "embedded-lab-control[{extra}]"' in message
        assert "pipx inject embedded-lab-control" in message
        assert "\n" not in message

    def test_pipx_quotes_version_specifiers(self):
        """`mcp>=2.2,<3` unquoted would be a shell redirection."""
        assert '"mcp>=2.2,<3"' in missing_extra_message("mcp")

    def test_sdwire_on_312_gives_install_command(self, monkeypatch):
        _fake_python(monkeypatch, 3, 12, 1)
        message = missing_extra_message("sdwire")
        assert 'pip install "embedded-lab-control[sdwire]"' in message
        assert '"sdwire>=0.3"' in message
        assert message.endswith("; sdwire needs Python 3.12+")

    def test_sdwire_on_old_python_leads_with_version(self, monkeypatch):
        """Below 3.12 no install command can work, so none is given."""
        _fake_python(monkeypatch, 3, 10, 14)
        message = missing_extra_message("sdwire")
        assert message.startswith(
            "SDWire support needs Python 3.12+ (this is Python 3.10)"
        )
        assert "installs nothing" in message
        assert "pip install" not in message
        assert "pipx" not in message

    def test_kasa_pin_follows_the_interpreter(self, monkeypatch):
        _fake_python(monkeypatch, 3, 10, 14)
        assert '"python-kasa>=0.7.7,<0.8"' in missing_extra_message("kasa")
        _fake_python(monkeypatch, 3, 11, 9)
        assert '"python-kasa>=0.12"' in missing_extra_message("kasa")

    def test_custom_description(self):
        message = missing_extra_message("web", what="Password hashing (werkzeug)")
        assert message.startswith("Password hashing (werkzeug) is not installed")
        assert "embedded-lab-control[web]" in message

    def test_error_is_runtime_error_with_extra(self):
        e = MissingExtraError("kasa")
        assert isinstance(e, RuntimeError)
        assert e.extra == "kasa"
        assert str(e) == missing_extra_message("kasa")

    def test_extras_match_pyproject(self):
        """Every user-facing extra in pyproject has a message, and no
        message names an extra that doesn't exist."""
        assert set(EXTRAS) == set(_pyproject_extras()) - {"dev"}

    @pytest.mark.parametrize("python", ["3.10", "3.11", "3.12", "3.13"])
    @pytest.mark.parametrize("extra", sorted(EXTRAS))
    def test_pipx_specs_match_pyproject(self, extra, python, monkeypatch):
        """The packages offered for `pipx inject` are the extra's own
        requirements, version floors included, for that Python (sdwire is
        not offered below 3.12: its message has no install command)."""
        if extra == "sdwire" and python < "3.12":
            assert _declared(extra, python) == set()
            return
        major, minor = map(int, python.split("."))
        _fake_python(monkeypatch, major, minor, 0)
        assert _offered(extra) == _declared(extra, python)


class TestIsMissing:
    def test_matches_module_and_submodules(self):
        assert is_missing(ModuleNotFoundError(name="flask"), "flask")
        assert is_missing(ModuleNotFoundError(name="mcp.server"), "mcp")

    def test_other_modules_do_not_match(self):
        assert not is_missing(ModuleNotFoundError(name="mcpx"), "mcp")
        assert not is_missing(ModuleNotFoundError(name="werkzeug"), "flask")
        assert not is_missing(ModuleNotFoundError(), "flask")

    def test_cannot_import_name_is_not_missing(self):
        """An installed but incompatible package (plain ImportError)
        keeps its real cause."""
        assert not is_missing(
            ImportError("cannot import name 'X'", name="kasa"), "kasa"
        )


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


class TestUserCommands:
    """`labctl user` hashes passwords with werkzeug, which comes with
    Flask in the [web] extra."""

    @pytest.mark.parametrize(
        "args", [["hash-password"], ["add", "alice"], ["verify", "alice"]]
    )
    def test_missing_werkzeug_gives_install_hint(self, args):
        with (
            patch.dict("sys.modules", {"werkzeug": None, "werkzeug.security": None}),
            patch("getpass.getpass") as getpass,
        ):
            result = CliRunner().invoke(main, ["user", *args])
        assert result.exit_code == 1
        assert "Password hashing (werkzeug) is not installed" in result.output
        assert 'pip install "embedded-lab-control[web]"' in result.output
        assert "Traceback" not in result.output
        getpass.assert_not_called()  # refused before asking for a password


def _module(name):
    import types

    return types.ModuleType(name)


class TestSdwireCommand:
    def test_discover_without_sdwire(self, tmp_path, monkeypatch):
        _fake_python(monkeypatch, 3, 12, 0)
        config = tmp_path / "config.yaml"
        config.write_text(f"database_path: {tmp_path / 'labctl.db'}\n")
        absent = {"sdwire": None, "sdwire.backend": None, "sdwire.backend.detect": None}
        with patch.dict("sys.modules", absent):
            result = CliRunner().invoke(main, ["-c", str(config), "sdwire", "discover"])
        assert result.exit_code == 1
        assert 'pip install "embedded-lab-control[sdwire]"' in result.output
        assert "Python 3.12+" in result.output
        assert "Traceback" not in result.output


class TestOtherImportErrorsKeepTheirCause:
    """Only the extra's own package being absent is reported as a missing
    extra; an installed but broken or incompatible one is not."""

    def test_sdwire_incompatible(self):
        from labctl.sdwire.controller import SDWireController, discover_sdwire_devices

        present = {
            "sdwire": _module("sdwire"),
            "sdwire.backend": _module("sdwire.backend"),
            "sdwire.backend.detect": _module("sdwire.backend.detect"),
        }
        with patch.dict("sys.modules", present):
            with pytest.raises(ImportError) as info:
                discover_sdwire_devices()
            assert not isinstance(info.value, MissingExtraError)
            with pytest.raises(ImportError):
                SDWireController("any")._get_device()

    def test_kasa_incompatible(self):
        from labctl.power import kasa as kasa_mod

        kasa_mod._get_cached_kasa_credentials.cache_clear()
        controller = kasa_mod.KasaController("192.0.2.78")
        try:
            with (
                patch.dict("sys.modules", {"kasa": _module("kasa")}),
                patch.object(controller, "_load_credentials", return_value=None),
            ):
                with pytest.raises(ImportError, match="cannot import name"):
                    controller.power_on()
        finally:
            kasa_mod._get_cached_kasa_credentials.cache_clear()

    def test_pyserial_dependency_missing(self, monkeypatch):
        import builtins

        from labctl.actuators import Transport
        from labctl.actuators.lcus1 import Lcus1SerialDriver

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "serial":
                raise ModuleNotFoundError("No module named 'termios'", name="termios")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        driver = Lcus1SerialDriver(expected_channel_count=1)
        with pytest.raises(ModuleNotFoundError, match="termios"):
            driver.open(Transport(device_path="/nonexistent/relay"))
        driver._transport = Transport(device_path="/nonexistent/relay")
        with pytest.raises(ModuleNotFoundError, match="termios"):
            driver.probe()


class TestKasa:
    def test_missing_kasa_is_not_cached(self):
        """A missing python-kasa skips credentials without caching that
        outcome for the rest of the process."""
        from labctl.power import kasa as kasa_mod

        kasa_mod._get_cached_kasa_credentials.cache_clear()
        controller = kasa_mod.KasaController("192.0.2.79")
        try:
            with patch.dict("sys.modules", {"kasa": None}):
                assert controller._load_credentials() is None
            assert kasa_mod._get_cached_kasa_credentials.cache_info().currsize == 0
        finally:
            kasa_mod._get_cached_kasa_credentials.cache_clear()

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
