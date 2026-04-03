"""Tests for serial capture and send utilities."""

import socket
import threading
import time

import pytest

from labctl.serial.capture import (
    CaptureResult,
    SendResult,
    capture_serial_output,
    resolve_port,
    send_serial_data,
)


class FakeTCPServer:
    """Simple TCP server for testing serial capture/send.

    Args:
        response_lines: Lines to send to connected clients.
        delay: Seconds to wait before sending response.
        wait_for_data: If True, wait for incoming data before responding.
            Needed for send-then-capture tests.
    """

    def __init__(self, response_lines=None, delay=0, wait_for_data=False):
        self.response_lines = response_lines or []
        self.delay = delay
        self.wait_for_data = wait_for_data
        self.received_data = b""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("localhost", 0))
        self.port = self.server_socket.getsockname()[1]
        self.server_socket.listen(1)
        self._thread = None
        self._client = None

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            self._client, _ = self.server_socket.accept()

            if self.wait_for_data:
                # Wait for client to send something before responding
                self._client.settimeout(5.0)
                try:
                    self.received_data = self._client.recv(4096)
                except socket.timeout:
                    pass

            if self.delay:
                time.sleep(self.delay)

            for line in self.response_lines:
                self._client.sendall((line + "\n").encode())
                time.sleep(0.01)

            # Keep connection open for client to read
            time.sleep(1.0)
        except Exception:
            pass
        finally:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass

    def stop(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self.server_socket.close()
        if self._thread:
            self._thread.join(timeout=2)


class TestCaptureResult:
    """Tests for CaptureResult."""

    def test_to_mcp_string_with_pattern(self):
        result = CaptureResult(
            output="line1\nline2",
            lines=2,
            pattern_matched=True,
            elapsed_seconds=3.5,
        )
        s = result.to_mcp_string(pattern="line2")
        assert "pattern 'line2' matched" in s
        assert "2 lines" in s
        assert "3.5s" in s
        assert "line1\nline2" in s

    def test_to_mcp_string_timeout(self):
        result = CaptureResult(
            output="partial",
            lines=1,
            pattern_matched=False,
            elapsed_seconds=15.0,
        )
        s = result.to_mcp_string(pattern="expected")
        assert "timeout" in s


class TestSendResult:
    """Tests for SendResult."""

    def test_to_mcp_string_no_capture(self):
        result = SendResult(sent=True, bytes_sent=10)
        assert "10 bytes" in result.to_mcp_string()

    def test_to_mcp_string_with_capture(self):
        capture = CaptureResult(
            output="response", lines=1,
            pattern_matched=True, elapsed_seconds=1.0,
        )
        result = SendResult(sent=True, bytes_sent=5, capture=capture)
        s = result.to_mcp_string()
        assert "response" in s


class TestResolvePort:
    """Tests for resolve_port."""

    def test_resolve_by_alias(self):
        from unittest.mock import MagicMock

        manager = MagicMock()
        fake_port = MagicMock(tcp_port=4000)
        manager.get_serial_port_by_alias.return_value = fake_port

        port = resolve_port(manager, "pi-5-1-console")
        assert port == fake_port
        manager.get_serial_port_by_alias.assert_called_once_with("pi-5-1-console")

    def test_resolve_by_sbc_name(self):
        from unittest.mock import MagicMock

        manager = MagicMock()
        manager.get_serial_port_by_alias.return_value = None

        fake_port = MagicMock(tcp_port=4000)
        fake_sbc = MagicMock(console_port=fake_port)
        manager.get_sbc_by_name.return_value = fake_sbc

        port = resolve_port(manager, "pi-5-1")
        assert port == fake_port

    def test_resolve_sbc_no_console(self):
        from unittest.mock import MagicMock

        manager = MagicMock()
        manager.get_serial_port_by_alias.return_value = None
        manager.get_sbc_by_name.return_value = MagicMock(console_port=None)

        with pytest.raises(ValueError, match="no console port"):
            resolve_port(manager, "pi-5-1")

    def test_resolve_not_found(self):
        from unittest.mock import MagicMock

        manager = MagicMock()
        manager.get_serial_port_by_alias.return_value = None
        manager.get_sbc_by_name.return_value = None

        with pytest.raises(ValueError, match="not a known"):
            resolve_port(manager, "nonexistent")


class TestCaptureSerialOutput:
    """Tests for capture_serial_output with a real TCP server."""

    def test_capture_basic(self):
        server = FakeTCPServer(response_lines=["Hello", "World"])
        server.start()
        try:
            result = capture_serial_output(
                "localhost", server.port, timeout=2.0
            )
            assert result.lines >= 2
            assert "Hello" in result.output
            assert "World" in result.output
        finally:
            server.stop()

    def test_capture_with_pattern(self):
        server = FakeTCPServer(
            response_lines=["booting...", "init done", "slmos>"]
        )
        server.start()
        try:
            result = capture_serial_output(
                "localhost", server.port,
                timeout=5.0,
                until_pattern="slmos>",
            )
            assert result.pattern_matched is True
            assert "slmos>" in result.output
        finally:
            server.stop()

    def test_capture_pattern_not_found(self):
        server = FakeTCPServer(response_lines=["line1", "line2"])
        server.start()
        try:
            result = capture_serial_output(
                "localhost", server.port,
                timeout=1.0,
                until_pattern="never_match",
            )
            assert result.pattern_matched is False
        finally:
            server.stop()

    def test_capture_tail(self):
        server = FakeTCPServer(
            response_lines=[f"line{i}" for i in range(10)]
        )
        server.start()
        try:
            result = capture_serial_output(
                "localhost", server.port,
                timeout=2.0,
                tail=3,
            )
            assert result.lines == 3
            assert "line9" in result.output
        finally:
            server.stop()

    def test_capture_timeout(self):
        server = FakeTCPServer(response_lines=[], delay=5)
        server.start()
        try:
            result = capture_serial_output(
                "localhost", server.port, timeout=0.5
            )
            assert result.elapsed_seconds >= 0.4
            assert result.pattern_matched is False
        finally:
            server.stop()

    def test_capture_pattern_in_partial_line(self):
        """Test pattern match on prompt without trailing newline."""
        # Server sends "slmos>" without a newline — like a real prompt
        server = FakeTCPServer(response_lines=[])
        server.start()

        # Manually send data without trailing newline via the server
        import threading
        def send_prompt():
            time.sleep(0.1)
            if server._client:
                try:
                    server._client.sendall(b"booting...\nslmos>")
                except Exception:
                    pass

        # We need a different approach — use a custom server
        server.stop()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("localhost", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def serve():
            client, _ = srv.accept()
            # Send a line with newline, then a prompt without newline
            client.sendall(b"booting...\nslmos>")
            time.sleep(2)
            client.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            result = capture_serial_output(
                "localhost", port,
                timeout=5.0,
                until_pattern="slmos>",
            )
            assert result.pattern_matched is True
            assert result.elapsed_seconds < 4.0  # Should not wait full timeout
            assert "slmos>" in result.output
        finally:
            srv.close()
            t.join(timeout=2)

    def test_capture_connection_refused(self):
        with pytest.raises(RuntimeError, match="Cannot connect"):
            capture_serial_output("localhost", 19999, timeout=1.0)


class TestSendSerialData:
    """Tests for send_serial_data."""

    def test_send_basic(self):
        server = FakeTCPServer()
        server.start()
        try:
            result = send_serial_data(
                "localhost", server.port, "hello"
            )
            assert result.sent is True
            assert result.bytes_sent == 7  # "hello\r\n"
        finally:
            server.stop()

    def test_send_raw(self):
        server = FakeTCPServer()
        server.start()
        try:
            result = send_serial_data(
                "localhost", server.port, "raw", newline=False
            )
            assert result.bytes_sent == 3  # "raw"
        finally:
            server.stop()

    def test_send_and_capture(self):
        server = FakeTCPServer(
            response_lines=["response1", "response2"],
            wait_for_data=True,
        )
        server.start()
        try:
            result = send_serial_data(
                "localhost", server.port,
                "cmd",
                capture_timeout=2.0,
            )
            assert result.sent is True
            assert result.capture is not None
            assert result.capture.lines >= 1
        finally:
            server.stop()

    def test_send_and_capture_with_pattern(self):
        server = FakeTCPServer(
            response_lines=["processing...", "done>"],
            wait_for_data=True,
        )
        server.start()
        try:
            result = send_serial_data(
                "localhost", server.port,
                "cmd",
                capture_timeout=5.0,
                capture_until="done>",
            )
            assert result.capture is not None
            assert result.capture.pattern_matched is True
        finally:
            server.stop()

    def test_send_connection_refused(self):
        with pytest.raises(RuntimeError, match="Cannot connect"):
            send_serial_data("localhost", 19999, "hello")
