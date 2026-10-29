"""Tests for named serial keys (issue #33)."""

import pytest

from labctl.serial.keys import decode_hex, encode_key, encode_keys


class TestEncodeKey:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("ESC", b"\x1b"),
            ("Escape", b"\x1b"),
            ("enter", b"\r"),
            ("Return", b"\r"),
            ("Tab", b"\t"),
            ("Space", b" "),
            ("Backspace", b"\x08"),
            ("Up", b"\x1b[A"),
            ("Down", b"\x1b[B"),
            ("Right", b"\x1b[C"),
            ("Left", b"\x1b[D"),
            ("Home", b"\x1b[H"),
            ("End", b"\x1b[F"),
            ("Insert", b"\x1b[2~"),
            ("Delete", b"\x1b[3~"),
            ("PgUp", b"\x1b[5~"),
            ("PageDown", b"\x1b[6~"),
            ("F1", b"\x1bOP"),
            ("F4", b"\x1bOS"),
            ("F5", b"\x1b[15~"),
            ("F10", b"\x1b[21~"),
            ("F11", b"\x1b[23~"),
            ("F12", b"\x1b[24~"),
            ("Ctrl-A", b"\x01"),
            ("ctrl-c", b"\x03"),
            ("Ctrl-H", b"\x08"),
            ("Ctrl-Z", b"\x1a"),
            ("Ctrl+C", b"\x03"),
            ("^C", b"\x03"),
            ("Control-D", b"\x04"),
            ("Page Up", b"\x1b[5~"),
            ("Del", b"\x1b[3~"),
            ("Ins", b"\x1b[2~"),
        ],
    )
    def test_known_keys(self, name, expected):
        assert encode_key(name) == expected

    @pytest.mark.parametrize(
        "name", ["F13", "Ctrl-1", "Ctrl-", "Ctrl-AB", "^", "", "\x1b", 3]
    )
    def test_unknown_keys_refused(self, name):
        with pytest.raises(ValueError, match="unknown key"):
            encode_key(name)

    def test_sequence_in_order(self):
        assert encode_keys(["Down", "Down", "Enter"]) == [b"\x1b[B", b"\x1b[B", b"\r"]


class TestDecodeHex:
    def test_plain_and_spaced(self):
        assert decode_hex("1b5b32337e") == b"\x1b[23~"
        assert decode_hex("1B 5b 32") == b"\x1b[2"

    @pytest.mark.parametrize("text", ["1", "zz", "0x1b", "1b5"])
    def test_malformed_refused(self, text):
        with pytest.raises(ValueError, match="data_hex"):
            decode_hex(text)
