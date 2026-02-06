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
    """
    import tty
    import termios
    import select

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)

        if ch == "\x1b":
            # Could be ESC or start of arrow sequence
            # Wait briefly to see if more chars follow
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return (Key.UP, "")
                    elif ch3 == "B":
                        return (Key.DOWN, "")
                    # Consume any remaining sequence chars
                    return (Key.ESC, "")
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
