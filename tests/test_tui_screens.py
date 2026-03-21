"""Tests for onboarding screens."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from ragnarbot.cli.tui.keys import Key, clear_key_reader, set_key_reader
from ragnarbot.cli.tui.screens import lightning_mode_screen, summary_screen


def make_console():
    return Console(file=StringIO(), force_terminal=True, width=100)


@pytest.fixture(autouse=True)
def cleanup_key_reader():
    yield
    clear_key_reader()


def test_summary_screen_shows_lightning_no_effect_note():
    set_key_reader(lambda: (Key.ENTER, ""))
    console = make_console()

    result = summary_screen(
        console,
        provider_name="Anthropic",
        auth_method="api_key",
        model_name="Claude Opus 4.6",
        model_id="anthropic/claude-opus-4-6",
        lightning_mode=True,
        telegram_configured=False,
    )

    assert result is True
    rendered = console.file.getvalue()
    assert "Lightning:" in rendered
    assert "Enabled" in rendered
    assert "Currently has no effect" in rendered


def test_summary_screen_omits_lightning_no_effect_note_for_openai_oauth():
    set_key_reader(lambda: (Key.ENTER, ""))
    console = make_console()

    with patch("ragnarbot.cli.tui.screens.is_codex_cli_available", return_value=True):
        result = summary_screen(
            console,
            provider_name="OpenAI",
            auth_method="oauth",
            model_name="GPT-5.4",
            model_id="openai/gpt-5.4",
            lightning_mode=True,
            telegram_configured=False,
        )

    assert result is True
    rendered = console.file.getvalue()
    assert "Lightning:" in rendered
    assert "Enabled" in rendered
    assert "Currently has no effect" not in rendered


def test_lightning_mode_screen_shows_codex_note_for_openai_oauth_when_missing():
    set_key_reader(lambda: (Key.ENTER, ""))
    console = make_console()

    with patch("ragnarbot.cli.tui.screens.is_codex_cli_available", return_value=False):
        result = lightning_mode_screen(console, auth_method="oauth", model_id="openai/gpt-5.4")

    assert result == 0
    rendered = console.file.getvalue()
    assert "Requires Codex CLI installed locally" in rendered
    assert "OpenAI OAuth Lightning requires Codex CLI installed locally." in rendered


def test_summary_screen_shows_codex_note_for_openai_oauth_when_missing():
    set_key_reader(lambda: (Key.ENTER, ""))
    console = make_console()

    with patch("ragnarbot.cli.tui.screens.is_codex_cli_available", return_value=False):
        result = summary_screen(
            console,
            provider_name="OpenAI",
            auth_method="oauth",
            model_name="GPT-5.4",
            model_id="openai/gpt-5.4",
            lightning_mode=True,
            telegram_configured=False,
        )

    assert result is True
    rendered = console.file.getvalue()
    assert "Lightning:" in rendered
    assert "Enabled" in rendered
    assert "OpenAI OAuth Lightning requires Codex CLI installed locally." in rendered
