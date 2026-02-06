"""Tests for TUI components with injected key reader."""

import pytest
from io import StringIO
from rich.console import Console

from ragnarbot.cli.tui.keys import Key, set_key_reader, clear_key_reader
from ragnarbot.cli.tui.components import (
    select_menu,
    text_input,
    info_screen,
    QuitOnboarding,
)


def make_console():
    """Create a console that writes to a string buffer."""
    return Console(file=StringIO(), force_terminal=True, width=80)


def make_key_sequence(keys: list[tuple[Key, str]]):
    """Create a key reader that returns keys from a list."""
    it = iter(keys)
    def reader():
        return next(it)
    return reader


@pytest.fixture(autouse=True)
def cleanup_key_reader():
    """Ensure key reader is cleared after each test."""
    yield
    clear_key_reader()


class TestSelectMenu:
    def test_select_first_option(self):
        set_key_reader(make_key_sequence([
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = select_menu(con, "Pick", [("A", "desc a"), ("B", "desc b")])
        assert result == 0

    def test_navigate_down_and_select(self):
        set_key_reader(make_key_sequence([
            (Key.DOWN, ""),
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = select_menu(con, "Pick", [("A", "a"), ("B", "b")])
        assert result == 1

    def test_navigate_wraps_around(self):
        set_key_reader(make_key_sequence([
            (Key.DOWN, ""),
            (Key.DOWN, ""),  # wraps to 0
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = select_menu(con, "Pick", [("A", "a"), ("B", "b")])
        assert result == 0

    def test_navigate_up_wraps(self):
        set_key_reader(make_key_sequence([
            (Key.UP, ""),  # wraps to last
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = select_menu(con, "Pick", [("A", "a"), ("B", "b"), ("C", "c")])
        assert result == 2

    def test_esc_returns_none(self):
        set_key_reader(make_key_sequence([
            (Key.ESC, ""),
        ]))
        con = make_console()
        result = select_menu(con, "Pick", [("A", "a")])
        assert result is None

    def test_q_raises_quit(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "q"),
        ]))
        con = make_console()
        with pytest.raises(QuitOnboarding):
            select_menu(con, "Pick", [("A", "a")])

    def test_capital_q_raises_quit(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "Q"),
        ]))
        con = make_console()
        with pytest.raises(QuitOnboarding):
            select_menu(con, "Pick", [("A", "a")])


class TestTextInput:
    def test_type_and_enter(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "h"),
            (Key.CHAR, "i"),
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name")
        assert result == "hi"

    def test_backspace(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "a"),
            (Key.CHAR, "b"),
            (Key.BACKSPACE, ""),
            (Key.CHAR, "c"),
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name")
        assert result == "ac"

    def test_esc_returns_none(self):
        set_key_reader(make_key_sequence([
            (Key.ESC, ""),
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name")
        assert result is None

    def test_empty_enter_shows_error_then_esc(self):
        set_key_reader(make_key_sequence([
            (Key.ENTER, ""),   # empty, error shown
            (Key.ESC, ""),     # go back
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name")
        assert result is None

    def test_allow_empty(self):
        set_key_reader(make_key_sequence([
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name", allow_empty=True)
        assert result == ""

    def test_q_with_existing_buffer(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "a"),
            (Key.CHAR, "q"),  # buffer is "a", so q is just appended
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = text_input(con, "Input", "Name")
        assert result == "aq"

    def test_q_empty_raises_quit(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "q"),
        ]))
        con = make_console()
        with pytest.raises(QuitOnboarding):
            text_input(con, "Input", "Name")


class TestInfoScreen:
    def test_enter_returns_true(self):
        set_key_reader(make_key_sequence([
            (Key.ENTER, ""),
        ]))
        con = make_console()
        result = info_screen(con, "Info", ["line1", "line2"])
        assert result is True

    def test_esc_returns_false(self):
        set_key_reader(make_key_sequence([
            (Key.ESC, ""),
        ]))
        con = make_console()
        result = info_screen(con, "Info", ["line1"])
        assert result is False

    def test_q_raises_quit(self):
        set_key_reader(make_key_sequence([
            (Key.CHAR, "q"),
        ]))
        con = make_console()
        with pytest.raises(QuitOnboarding):
            info_screen(con, "Info", ["line1"])
