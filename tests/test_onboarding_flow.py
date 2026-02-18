"""Integration tests for onboarding flow."""

import json
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest
from rich.console import Console

from ragnarbot.cli.tui.keys import Key, set_key_reader, clear_key_reader
from ragnarbot.cli.tui import run_onboarding
from ragnarbot.cli.tui.components import QuitOnboardingError
from ragnarbot.config.schema import Config
from ragnarbot.auth.credentials import Credentials


def make_console():
    return Console(file=StringIO(), force_terminal=True, width=80)


def make_key_sequence(keys):
    it = iter(keys)
    def reader():
        return next(it)
    return reader


# Common key sequences for skipping web search (select "Skip" = 3rd option)
SKIP_WEB_SEARCH = [
    (Key.DOWN, ""),          # Past Brave Search
    (Key.DOWN, ""),          # To Skip
    (Key.ENTER, ""),         # Select Skip
]


@pytest.fixture(autouse=True)
def cleanup():
    yield
    clear_key_reader()


def _patches(tmp_path):
    """Return a dict of patches for _save_results dependencies."""
    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "creds.json"
    workspace = tmp_path / "workspace"

    config = Config()
    creds = Credentials()

    return {
        "load_config": patch(
            "ragnarbot.config.loader.load_config", return_value=config
        ),
        "save_config": patch("ragnarbot.config.loader.save_config"),
        "get_config_path": patch(
            "ragnarbot.config.loader.get_config_path", return_value=config_path
        ),
        "load_credentials": patch(
            "ragnarbot.auth.credentials.load_credentials", return_value=creds
        ),
        "save_credentials": patch("ragnarbot.auth.credentials.save_credentials"),
        "get_credentials_path": patch(
            "ragnarbot.auth.credentials.get_credentials_path", return_value=creds_path
        ),
        "get_workspace_path": patch(
            "ragnarbot.utils.helpers.get_workspace_path", return_value=workspace
        ),
        "create_templates": patch("ragnarbot.cli.commands._create_workspace_templates"),
    }


class TestOnboardingFlow:
    def _run_with_keys(self, keys, tmp_path):
        """Helper: run onboarding with injected keys and temp config paths."""
        set_key_reader(make_key_sequence(keys))
        con = make_console()

        p = _patches(tmp_path)
        with (
            p["load_config"] as mock_load_config,
            p["save_config"] as mock_save_config,
            p["get_config_path"],
            p["load_credentials"] as mock_load_creds,
            p["save_credentials"] as mock_save_creds,
            p["get_credentials_path"],
            p["get_workspace_path"],
            p["create_templates"],
        ):
            run_onboarding(con)
            return mock_save_config, mock_save_creds

    def test_anthropic_api_key_flow(self, tmp_path):
        """Full flow: Anthropic -> API Key -> token -> first model -> skip all optional -> save."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic (first option)
            (Key.DOWN, ""),          # Navigate to API Key
            (Key.ENTER, ""),         # Select API Key
            *[(Key.CHAR, c) for c in "sk-ant-test-key-123"],
            (Key.ENTER, ""),         # Confirm key
            (Key.ENTER, ""),         # Select first model (Opus)
            (Key.ENTER, ""),         # Skip telegram (empty enter)
            (Key.DOWN, ""),          # Voice: past ElevenLabs
            (Key.DOWN, ""),          # Voice: to Skip
            (Key.ENTER, ""),         # Select Skip
            *SKIP_WEB_SEARCH,       # Skip web search
            (Key.DOWN, ""),          # Navigate to "No" (manual start)
            (Key.ENTER, ""),         # Select manual start
            (Key.ENTER, ""),         # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        assert mock_save_config.called
        config = mock_save_config.call_args[0][0]
        assert config.agents.defaults.model == "anthropic/claude-opus-4-6"
        assert config.agents.defaults.auth_method == "api_key"

        assert mock_save_creds.called
        creds = mock_save_creds.call_args[0][0]
        assert creds.providers.anthropic.api_key == "sk-ant-test-key-123"

    def test_anthropic_oauth_flow(self, tmp_path):
        """Full flow: Anthropic -> OAuth -> token -> second model -> skip all optional."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.ENTER, ""),        # Select OAuth (first option)
            *[(Key.CHAR, c) for c in "oauth-token-xyz"],
            (Key.ENTER, ""),        # Confirm token
            (Key.DOWN, ""),         # Navigate to Sonnet
            (Key.ENTER, ""),        # Select Sonnet
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.agents.defaults.model == "anthropic/claude-sonnet-4-6"
        assert config.agents.defaults.auth_method == "oauth"

        creds = mock_save_creds.call_args[0][0]
        assert creds.providers.anthropic.oauth_key == "oauth-token-xyz"

    def test_openai_api_key_flow(self, tmp_path):
        """OpenAI with API Key auth — select auth method, then provide token."""
        keys = [
            (Key.DOWN, ""),         # Navigate to OpenAI
            (Key.ENTER, ""),        # Select OpenAI
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-openai-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model (GPT-5.2)
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.agents.defaults.model == "openai/gpt-5.2"
        assert config.agents.defaults.auth_method == "api_key"

        creds = mock_save_creds.call_args[0][0]
        assert creds.providers.openai.api_key == "sk-openai-key"

    def test_gemini_flow(self, tmp_path):
        """Gemini flow with API Key auth and Flash model selected."""
        keys = [
            (Key.DOWN, ""),         # Past OpenAI
            (Key.DOWN, ""),         # To Gemini
            (Key.ENTER, ""),        # Select Gemini
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "AIza-gemini-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.DOWN, ""),         # Navigate to Flash
            (Key.ENTER, ""),        # Select Flash
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.agents.defaults.model == "gemini/gemini-3-flash-preview"

        creds = mock_save_creds.call_args[0][0]
        assert creds.providers.gemini.api_key == "AIza-gemini-key"

    def test_back_navigation(self, tmp_path):
        """Test Esc goes back one screen."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.ENTER, ""),        # Select OAuth
            (Key.ESC, ""),          # Back to auth method
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.agents.defaults.auth_method == "api_key"

    def test_quit_from_provider_screen(self, tmp_path):
        """Q on first screen should exit cleanly."""
        keys = [
            (Key.CHAR, "q"),
        ]
        set_key_reader(make_key_sequence(keys))
        con = make_console()

        p = _patches(tmp_path)
        with (
            p["load_config"],
            p["save_config"] as mock_save,
            p["get_config_path"],
            p["load_credentials"],
            p["save_credentials"],
            p["get_credentials_path"],
            p["get_workspace_path"],
            p["create_templates"],
        ):
            run_onboarding(con)
            assert not mock_save.called

    def test_esc_from_first_screen_exits(self, tmp_path):
        """Esc on provider screen should quit (no previous screen)."""
        keys = [
            (Key.ESC, ""),
        ]
        set_key_reader(make_key_sequence(keys))
        con = make_console()

        p = _patches(tmp_path)
        with (
            p["load_config"],
            p["save_config"] as mock_save,
            p["get_config_path"],
            p["load_credentials"],
            p["save_credentials"],
            p["get_credentials_path"],
            p["get_workspace_path"],
            p["create_templates"],
        ):
            run_onboarding(con)
            assert not mock_save.called


class TestTelegramValidation:
    def test_telegram_valid_token(self, tmp_path):
        """Valid telegram token should be accepted."""
        keys = [
            (Key.ENTER, ""),        # Anthropic
            (Key.DOWN, ""),
            (Key.ENTER, ""),        # API Key
            *[(Key.CHAR, c) for c in "sk-key"],
            (Key.ENTER, ""),
            (Key.ENTER, ""),        # First model
            # Telegram token
            *[(Key.CHAR, c) for c in "123456:ABC-DEF"],
            (Key.ENTER, ""),        # Submit token
            (Key.ENTER, ""),        # Accept bot info screen
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]

        set_key_reader(make_key_sequence(keys))
        con = make_console()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {"first_name": "TestBot", "username": "test_bot"},
        }

        p = _patches(tmp_path)
        with (
            p["load_config"],
            p["save_config"] as mock_sc,
            p["get_config_path"],
            p["load_credentials"],
            p["save_credentials"] as mock_scr,
            p["get_credentials_path"],
            p["get_workspace_path"],
            p["create_templates"],
            patch("ragnarbot.cli.tui.screens.httpx.get", return_value=mock_response),
        ):
            run_onboarding(con)

            config = mock_sc.call_args[0][0]
            assert config.channels.telegram.enabled is True

            creds = mock_scr.call_args[0][0]
            assert creds.channels.telegram.bot_token == "123456:ABC-DEF"


class TestVoiceTranscriptionOnboarding:
    def _run_with_keys(self, keys, tmp_path):
        """Helper: run onboarding with injected keys and temp config paths."""
        set_key_reader(make_key_sequence(keys))
        con = make_console()

        p = _patches(tmp_path)
        with (
            p["load_config"] as mock_load_config,
            p["save_config"] as mock_save_config,
            p["get_config_path"],
            p["load_credentials"] as mock_load_creds,
            p["save_credentials"] as mock_save_creds,
            p["get_credentials_path"],
            p["get_workspace_path"],
            p["create_templates"],
        ):
            run_onboarding(con)
            return mock_save_config, mock_save_creds

    def test_groq_voice_selection(self, tmp_path):
        """Select Groq for voice transcription and provide API key."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-ant-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs to Groq
            (Key.ENTER, ""),        # Select Groq
            *[(Key.CHAR, c) for c in "gsk-groq-key"],
            (Key.ENTER, ""),        # Confirm Groq key
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.transcription.provider == "groq"

        creds = mock_save_creds.call_args[0][0]
        assert creds.services.groq.api_key == "gsk-groq-key"

    def test_web_search_brave_with_key(self, tmp_path):
        """Select Brave Search and provide API key during onboarding."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-ant-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            (Key.ENTER, ""),        # Select Brave Search (first option)
            *[(Key.CHAR, c) for c in "BSA-brave-key"],
            (Key.ENTER, ""),        # Confirm Brave API key
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.tools.web.search.engine == "brave"

        creds = mock_save_creds.call_args[0][0]
        assert creds.services.brave_search.api_key == "BSA-brave-key"

    def test_web_search_duckduckgo(self, tmp_path):
        """Select DuckDuckGo search engine during onboarding (no API key needed)."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-ant-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            (Key.DOWN, ""),         # Web search: past Brave to DuckDuckGo
            (Key.ENTER, ""),        # Select DuckDuckGo
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        config = mock_save_config.call_args[0][0]
        assert config.tools.web.search.engine == "duckduckgo"

    def test_web_search_skip(self, tmp_path):
        """Skip web search during onboarding."""
        keys = [
            (Key.ENTER, ""),        # Select Anthropic
            (Key.DOWN, ""),         # Navigate to API Key
            (Key.ENTER, ""),        # Select API Key
            *[(Key.CHAR, c) for c in "sk-ant-key"],
            (Key.ENTER, ""),        # Confirm key
            (Key.ENTER, ""),        # Select first model
            (Key.ENTER, ""),        # Skip telegram
            (Key.DOWN, ""),         # Voice: past ElevenLabs
            (Key.DOWN, ""),         # Voice: to Skip
            (Key.ENTER, ""),        # Select Skip
            *SKIP_WEB_SEARCH,      # Skip web search
            (Key.DOWN, ""),         # Navigate to "No" (manual start)
            (Key.ENTER, ""),        # Select manual start
            (Key.ENTER, ""),        # Confirm summary
        ]
        mock_save_config, mock_save_creds = self._run_with_keys(keys, tmp_path)

        # Engine should remain default "brave" since we skipped (engine="none" means don't set)
        config = mock_save_config.call_args[0][0]
        assert config.tools.web.search.engine == "brave"  # default, unchanged
