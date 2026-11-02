"""
Shared pytest fixtures for labctl tests.
"""

import errno
import ipaddress
import os
import re
import site
import socket
import subprocess
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


def _words(text: str) -> list:
    """Words of a command line, with shell quoting and operators removed."""
    return [w for w in re.split(r"[\s;&|()`'\"\\]+", text) if w]


def _runs_sudo(args, *, shell: bool = False, executable=None) -> bool:
    """Whether a Popen call might start sudo (#50): the executable, or any
    word of the command (argv words, and every word of a shell line or
    ``sh -c`` script, with quotes and backslashes stripped), is ``sudo``.
    Deliberately broad: ``echo sudo`` counts too, which no test needs."""
    if executable is not None and _is_sudo(os.fsdecode(executable)):
        return True
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        argv = [os.fsdecode(a) for a in args]
    return any(_is_sudo(w) for arg in argv for w in _words(arg))


def _is_sudo(word: str) -> bool:
    return os.path.basename(word) == "sudo"


# The guard is installed when pytest starts (pytest_configure below), so it
# covers collection and fixtures of every scope. _SUDO_ALLOWED is switched
# per test by _sudo_policy; refusals in other threads are kept in
# _THREAD_HITS and fail the test at teardown.
_SUDO_ALLOWED = False
_THREAD_HITS: list = []
_REAL = {}


def _refuse(what) -> None:
    import threading

    message = (
        f"test started a real sudo: {what!r}. Mock the subprocess call, or "
        "mark the test @pytest.mark.allow_sudo (tests/conftest.py, #50)"
    )
    if threading.current_thread() is not threading.main_thread():
        _THREAD_HITS.append(message)
    pytest.fail(message)


def _install_sudo_guard() -> None:
    """Refuse any subprocess or ``os.system`` that would start a real
    ``sudo`` (#50). CI runners have passwordless sudo and a developer's
    machine may prompt, so a test that forgets to mock a ``sudo
    dd``/``mount``/``udevadm`` must not run it. Python subprocesses go
    through ``subprocess.Popen`` (``run``, ``check_output``, asyncio's
    ``create_subprocess_exec``); ``os.exec*``/``os.spawn*`` and pty helpers
    are not covered (labctl uses none). The refusal is a test failure
    (``pytest.fail``), which the code under test's ``except Exception``
    can't swallow."""
    import inspect

    real_init = _REAL["init"] = subprocess.Popen.__init__
    real_system = _REAL["system"] = os.system
    signature = inspect.signature(real_init)

    def guarded_init(self, *a, **kw):
        bound = signature.bind(self, *a, **kw)
        args = bound.arguments["args"]
        if not isinstance(args, (str, bytes, os.PathLike, list, tuple)):
            args = bound.arguments["args"] = list(args)  # don't use it up
        if not _SUDO_ALLOWED and _runs_sudo(
            args,
            shell=bool(bound.arguments.get("shell")),
            executable=bound.arguments.get("executable"),
        ):
            _refuse(args)
        return real_init(*bound.args, **bound.kwargs)

    def guarded_system(command):
        if not _SUDO_ALLOWED and _runs_sudo(command, shell=True):
            _refuse(command)
        return real_system(command)

    subprocess.Popen.__init__ = guarded_init
    os.system = guarded_system


def pytest_configure(config):
    _install_sudo_guard()


def pytest_unconfigure(config):
    if _REAL:
        subprocess.Popen.__init__ = _REAL["init"]
        os.system = _REAL["system"]


@pytest.fixture(autouse=True)
def _sudo_policy(request):
    """Let a test marked ``allow_sudo`` through the guard (only the test
    and its function-scoped fixtures: wider fixtures are set up before
    it), and fail a test whose background thread was refused."""
    global _SUDO_ALLOWED
    _SUDO_ALLOWED = request.node.get_closest_marker("allow_sudo") is not None
    _THREAD_HITS.clear()
    yield
    _SUDO_ALLOWED = False
    if _THREAD_HITS:
        hits = list(_THREAD_HITS)
        _THREAD_HITS.clear()
        pytest.fail("a background thread " + hits[0][len("test ") :])


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
