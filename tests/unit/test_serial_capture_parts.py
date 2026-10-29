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
