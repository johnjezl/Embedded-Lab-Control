"""Named keys for serial consoles, as the bytes a terminal sends.

Tool arguments are text written by a model, which can't reliably carry
raw control characters (an ESC is dropped before it reaches the server),
so MCP clients name keys instead (issue #33). Encodings are xterm's,
which VT100-style consoles (EDK2/UEFI, U-Boot, Linux) accept.
"""

from __future__ import annotations

_NAMED = {
    "esc": b"\x1b",
    "enter": b"\r",
    "tab": b"\t",
    "space": b" ",
    "backspace": b"\x7f",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "pgup": b"\x1b[5~",
    "pgdn": b"\x1b[6~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}
_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "pageup": "pgup",
    "pagedown": "pgdn",
}

KEY_NAMES = (
    "ESC, Enter, Tab, Space, Backspace (0x7F), Up, Down, Left, Right, "
    "Home, End, Insert, Delete, PgUp, PgDn, F1-F12, Ctrl-A..Ctrl-Z "
    "(0x01-0x1A; Ctrl-H is 0x08)"
)


def encode_key(name: str) -> bytes:
    """The bytes for key ``name`` (case-insensitive); ValueError if unknown."""
    key = name.strip().lower() if isinstance(name, str) else ""
    key = _ALIASES.get(key, key)
    if key in _NAMED:
        return _NAMED[key]
    if key.startswith("ctrl-") and len(key) == 6 and "a" <= key[5] <= "z":
        return bytes([ord(key[5]) - ord("a") + 1])
    raise ValueError(f"unknown key {name!r}; known keys: {KEY_NAMES}")


def encode_keys(names: list[str]) -> bytes:
    """The bytes for a sequence of key names, in order."""
    return b"".join(encode_key(n) for n in names)


def decode_hex(text: str) -> bytes:
    """Bytes from hex text, e.g. "1b5b32337e" or "1b 5b 32"; ValueError if
    malformed."""
    try:
        return bytes.fromhex(text)
    except (TypeError, ValueError):
        raise ValueError(
            f"data_hex must be pairs of hex digits (spaces allowed): {text!r}"
        ) from None
