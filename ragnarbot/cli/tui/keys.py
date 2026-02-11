"""Raw keyboard input handling for TUI."""

import sys
from enum import Enum


class Key(Enum):
    UP = "up"
    DOWN = "down"
    ENTER = "enter"
    ESC = "esc"
    BACKSPACE = "backspace"
    QUIT = "quit"
    CHAR = "char"


# Injectable key reader for testing
_key_reader = None

# Buffer for pasted characters (drained from fd before leaving raw mode)
_input_buffer: list[tuple[Key, str]] = []


def set_key_reader(fn):
    """Set a custom key reader function (for testing)."""
    global _key_reader
    _key_reader = fn


def clear_key_reader():
    """Clear the custom key reader."""
    global _key_reader
    _key_reader = None


def get_key_reader():
    """Get the current key reader function."""
    return _key_reader or read_key


def _byte_to_key(ch: str) -> tuple[Key, str]:
    """Convert a single character to a Key event."""
    if ch == "\r" or ch == "\n":
        return (Key.ENTER, "")
    if ch == "\x7f" or ch == "\x08":
        return (Key.BACKSPACE, "")
    if ch == "\x03":
        raise KeyboardInterrupt
    return (Key.CHAR, ch)


def read_key() -> tuple[Key, str]:
    """Read a single keypress from stdin.

    Returns (Key, char_value) where char_value is the actual character
    for Key.CHAR events, empty string otherwise.

    On paste (multiple bytes available at once), all bytes are drained
    from the fd while still in raw mode and buffered. Subsequent calls
    return from the buffer without re-entering raw mode.
    """
    global _input_buffer

    # Return buffered keys first (from a previous paste)
    if _input_buffer:
        return _input_buffer.pop(0)

    import os
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        ch = b.decode("utf-8", errors="replace")

        if ch == "\x1b":
            # Could be ESC or start of arrow/escape sequence
            if select.select([fd], [], [], 0.05)[0]:
                seq = os.read(fd, 2)
                if seq == b"[A":
                    return (Key.UP, "")
                elif seq == b"[B":
                    return (Key.DOWN, "")
                return (Key.ESC, "")
            return (Key.ESC, "")

        first = _byte_to_key(ch)

        # Drain any remaining bytes (paste buffer) while still in raw mode
        while select.select([fd], [], [], 0)[0]:
            more_b = os.read(fd, 1)
            more_ch = more_b.decode("utf-8", errors="replace")
            if more_ch == "\x1b":
                # Skip escape sequences embedded in paste
                if select.select([fd], [], [], 0.01)[0]:
                    os.read(fd, 2)
                continue
            if more_ch in ("\r", "\n"):
                continue  # skip newlines from terminal line-wrapping in paste
            _input_buffer.append(_byte_to_key(more_ch))

        return first

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
