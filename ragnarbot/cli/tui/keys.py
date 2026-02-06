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


def read_key() -> tuple[Key, str]:
    """Read a single keypress from stdin.

    Returns (Key, char_value) where char_value is the actual character
    for Key.CHAR events, empty string otherwise.

    Uses os.read() on the raw fd to avoid Python's buffered I/O
    swallowing escape sequence bytes.
    """
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
            # Wait briefly to see if more bytes follow on the fd
            if select.select([fd], [], [], 0.05)[0]:
                seq = os.read(fd, 2)
                if seq == b"[A":
                    return (Key.UP, "")
                elif seq == b"[B":
                    return (Key.DOWN, "")
                # Unknown escape sequence — consume and treat as ESC
                return (Key.ESC, "")
            return (Key.ESC, "")

        if ch == "\r" or ch == "\n":
            return (Key.ENTER, "")

        if ch == "\x7f" or ch == "\x08":
            return (Key.BACKSPACE, "")

        if ch == "\x03":
            # Ctrl+C
            raise KeyboardInterrupt

        if ord(ch) < 32:
            # Other control characters
            return (Key.CHAR, ch)

        return (Key.CHAR, ch)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
