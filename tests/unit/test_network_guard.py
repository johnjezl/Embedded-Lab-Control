"""The suite-wide network guard in tests/conftest.py (#49)."""

import errno
import socket

import pytest


def test_connect_to_lan_address_refused_at_once():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError) as exc:
            s.connect(("192.168.1.100", 80))
    assert exc.value.errno == errno.ENETUNREACH


def test_connect_ex_to_lan_address_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.connect_ex(("10.0.0.1", 80)) == errno.ENETUNREACH


def test_datagram_to_broadcast_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OSError):
            s.sendto(b"x", ("255.255.255.255", 9999))


def test_name_lookup_refused():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("plug-1", 80)


def test_other_lookups_refused():
    with pytest.raises(socket.gaierror):
        socket.gethostbyname("plug-1")
    with pytest.raises(socket.gaierror):
        socket.gethostbyaddr("plug-1")


def test_sendmsg_to_lan_address_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OSError):
            s.sendmsg([b"x"], [], 0, ("192.168.1.255", 9999))


@pytest.mark.parametrize("name", ["LOCALHOST", "localhost.", "Ip6-Localhost"])
def test_local_name_spellings_resolve(name):
    from tests.conftest import _is_local_host

    assert _is_local_host(name)


def test_ip_literal_lookup_allowed():
    """A literal is parsed, not looked up (binding code resolves them)."""
    assert socket.getaddrinfo("192.0.2.1", 80, type=socket.SOCK_STREAM)


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1", "0.0.0.0"]
)
def test_loopback_connections_allowed(host):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        server = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("address family unavailable")
    with server:
        if family == socket.AF_INET6:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        try:
            server.bind(("::" if family == socket.AF_INET6 else "127.0.0.1", 0))
        except OSError:
            pytest.skip("address family unavailable")
        server.listen(1)
        port = server.getsockname()[1]
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.connect((host, port))


def test_non_inet_address_passes_through():
    from tests.conftest import _is_loopback

    assert _is_loopback("/run/some.sock")
    assert _is_loopback((0, 0))  # AF_NETLINK (pid, groups)
    assert _is_loopback((b"127.0.0.1", 80))
    assert not _is_loopback((b"192.168.1.1", 80))


def test_unix_socket_not_checked(tmp_path):
    """Only AF_INET/AF_INET6 sockets are guarded."""
    path = str(tmp_path / "s")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
