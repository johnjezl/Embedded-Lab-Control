"""
Shared pytest fixtures for labctl tests.
"""

import errno
import ipaddress
import os
import site
import socket
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# Where packages installed with `pip install --user` live, computed from
# the real HOME before any test replaces it.
_USER_BASE = site.getuserbase()


@pytest.fixture
def _isolated_home(tmp_path):
    """This test's home directory (empty; created when something writes
    there)."""
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def _no_real_config(_isolated_home, monkeypatch):
    """Keep every test, and the processes it starts, away from the
    machine's real labctl files and from other tests' state.

    load_config() falls back to ~/.config/labctl/config.yaml and then
    /etc/labctl/config.yaml, whose database_path is the live database: on
    a lab host, a test that loads config without naming a file would
    open, and migrate, production. Point HOME and the XDG dirs at this
    test's own home and the system config at a file that doesn't exist
    (in this process and, via LABCTL_SYSTEM_CONFIG, in child processes),
    and drop LABCTL_* overrides from the developer's environment. Tests
    that need a config write one and name it.

    `labctl -c FILE mcp` writes LABCTL_CONFIG / _EXCLUSIVE straight into
    os.environ, and run_server() snapshots its config in
    labctl.core.config: both are registered with monkeypatch (set, then
    deleted) so they are undone after the test, as is the snapshot.
    """
    from labctl.core import config as config_mod

    for name in list(os.environ):
        if name.startswith("LABCTL_"):
            monkeypatch.delenv(name)
    for name in ("LABCTL_CONFIG", "LABCTL_CONFIG_EXCLUSIVE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    system = _isolated_home / "etc-labctl-config.yaml"
    monkeypatch.setenv("LABCTL_SYSTEM_CONFIG", str(system))
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG_FILE", system)
    monkeypatch.setattr(config_mod, "_snapshot", None)
    monkeypatch.setenv("HOME", str(_isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_isolated_home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(_isolated_home / ".local" / "share"))
    # Child Pythons would look for user site-packages under the new HOME.
    monkeypatch.setenv("PYTHONUSERBASE", _USER_BASE)


_LOCAL_NAMES = {"localhost", "ip6-localhost", "ip6-loopback", "localhost.localdomain"}
_INET_FAMILIES = (socket.AF_INET, socket.AF_INET6)


def _parse_host(host):
    """(ip address or None, normalized name) for a host given to socket."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    name = "" if host is None else str(host).lower().rstrip(".")
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]), name
    except ValueError:
        return None, name


def _is_local_host(host) -> bool:
    ip, name = _parse_host(host)
    if ip is None:  # a name to resolve: could be anywhere
        return name in _LOCAL_NAMES or name == ""
    if ip.is_unspecified:  # 0.0.0.0 / :: on Linux: this host
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not isinstance(address[0], (str, bytes)):
        return True
    return _is_local_host(address[0])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse connections, datagrams and name lookups for anything but
    loopback (#49).

    Fixture plugs and hosts use LAN addresses (192.168.1.x), which can be
    real devices on a lab network and otherwise cost a 5 s timeout each
    where nothing answers. Refusing at once with ENETUNREACH takes the
    same unreachable-device path in labctl without touching the network.
    Only IPv4/IPv6 sockets are checked (AF_UNIX, netlink, packet and CAN
    sockets pass), and IP literals may be resolved (they're parsed, not
    looked up; connect still checks them).
    """

    def refuse(address):
        raise OSError(
            errno.ENETUNREACH, f"tests may not reach {address!r} (tests/conftest.py)"
        )

    def refuse_name(host):
        if not _is_local_host(host) and _parse_host(host)[0] is None:
            raise socket.gaierror(
                socket.EAI_NONAME, f"tests may not resolve {host!r} (tests/conftest.py)"
            )

    def blocked(sock, address):
        return sock.family in _INET_FAMILIES and not _is_loopback(address)

    def guard_lookup(real):
        def lookup(host, *args, **kwargs):
            refuse_name(host)
            return real(host, *args, **kwargs)

        return lookup

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

    def connect(self, address):
        if blocked(self, address):
            refuse(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if blocked(self, address):
            return errno.ENETUNREACH
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        if blocked(self, args[-1]):
            refuse(args[-1])
        return real_sendto(self, data, *args)

    def sendmsg(self, buffers, *args):
        if len(args) >= 3 and args[2] is not None and blocked(self, args[2]):
            refuse(args[2])
        return real_sendmsg(self, buffers, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, guard_lookup(getattr(socket, name)))


@pytest.fixture
def no_sleep(monkeypatch):
    """``time.sleep`` returns at once: settle delays (power off, card
    switch, the 3 s minimum power cycle) don't have to pass for real (#49).
    Tests that check a delay patch ``time.sleep`` themselves on top."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)


class _FakeClock:
    """labctl's clock (``timeutil.now()``, aware UTC), stopped until
    ``advance``."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    """Freeze labctl's clock (``timeutil.now``); tests move it with
    ``clock.advance(s)`` instead of sleeping (#49, #63)."""
    fake = _FakeClock()
    monkeypatch.setattr("labctl.core.timeutil.now", lambda: fake.now)
    return fake


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


@pytest.fixture(autouse=True)
def _fresh_config_warnings():
    """Config warnings are logged once per process (labctl.core.config
    _warn_once); start every test with a clean slate so it sees its own."""
    from labctl.core import config

    config._warned.clear()
    yield
    config._warned.clear()


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


class _AbsentPackages:
    """Import hook that reports ``packages`` as not installed."""

    def __init__(self, packages):
        self.packages = set(packages)

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.packages:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@pytest.fixture
def absent_packages(monkeypatch):
    """Make top-level packages look uninstalled, as on a bare install.

    ``absent_packages("sdwire")`` drops ``sdwire`` and its submodules from
    ``sys.modules`` and blocks re-importing them, so ``from
    sdwire.backend.detect import ...`` raises ModuleNotFoundError naming
    ``sdwire`` itself (a None entry in sys.modules names the submodule
    instead). monkeypatch restores everything afterwards.
    """

    def block(*packages):
        for name in list(sys.modules):
            if any(name == p or name.startswith(p + ".") for p in packages):
                monkeypatch.delitem(sys.modules, name)
        monkeypatch.setattr(
            sys, "meta_path", [_AbsentPackages(packages), *sys.meta_path]
        )

    return block
