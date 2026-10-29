"""Multi-part serial sends (one part per named key, #33)."""

from unittest.mock import MagicMock, patch

from labctl.serial import capture


def test_parts_written_separately_with_a_pause():
    sock = MagicMock()
    with patch.object(capture.time, "sleep") as sleep:
        capture._write_payload(sock, [b"\x1b", b"\x1b[B", b"\r"])
    assert [c.args[0] for c in sock.sendall.call_args_list] == [
        b"\x1b",
        b"\x1b[B",
        b"\r",
    ]
    assert [c.args[0] for c in sleep.call_args_list] == [capture.PART_GAP_SECONDS] * 2


def test_send_counts_all_parts_and_appends_newline():
    sock = MagicMock()
    with (
        patch.object(capture.socket, "socket", return_value=sock),
        patch.object(capture.time, "sleep"),
    ):
        result = capture.send_serial_data("localhost", 1, [b"ab", b"c"], newline=True)
    assert result.bytes_sent == 5
    assert sock.sendall.call_args_list[-1].args[0] == b"\r\n"


def test_empty_part_list_sends_nothing():
    sock = MagicMock()
    with patch.object(capture.socket, "socket", return_value=sock):
        result = capture.send_serial_data("localhost", 1, [], newline=False)
    assert result.bytes_sent == 0
    sock.sendall.assert_not_called()


def test_send_and_capture_writes_parts():
    """keys plus capture_timeout (e.g. F11, then read the boot menu) goes
    through the capture path with a list of parts."""
    from tests.unit.test_serial_capture import FakeTCPServer

    server = FakeTCPServer(response_lines=["Boot Manager"], wait_for_data=True)
    server.start()
    try:
        with patch.object(
            capture, "_write_payload", wraps=capture._write_payload
        ) as write:
            result = capture.send_serial_data(
                "localhost",
                server.port,
                [b"\x1b", b"\x1b[B"],
                newline=False,
                capture_timeout=2.0,
                capture_until="Boot Manager",
            )
    finally:
        server.stop()
    assert write.call_args_list[0].args[1] == [b"\x1b", b"\x1b[B"]
    assert result.bytes_sent == 4
    assert result.capture is not None and result.capture.pattern_matched
