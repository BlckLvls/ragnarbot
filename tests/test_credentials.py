"""Tests for credential loading, saving, and permissions."""

import json
import os
import stat

from ragnarbot.auth.credentials import (
    Credentials,
    load_credentials,
    save_credentials,
)


def test_default_credentials():
    """Default credentials have empty values."""
    creds = Credentials()
    assert creds.providers.anthropic.api_key == ""
    assert creds.providers.anthropic.oauth_key == ""
    assert creds.services.groq.api_key == ""
    assert creds.services.elevenlabs.api_key == ""
    assert creds.channels.telegram.bot_token == ""


def test_save_load_round_trip(tmp_path):
    """Credentials survive a save/load round trip."""
    path = tmp_path / "credentials.json"

    creds = Credentials()
    creds.providers.anthropic.oauth_key = "sk-ant-oat-test"
    creds.providers.openai.api_key = "sk-openai-test"
    creds.services.web_search.api_key = "brave-key"
    creds.channels.telegram.bot_token = "bot123:ABC"

    save_credentials(creds, path)
    loaded = load_credentials(path)

    assert loaded.providers.anthropic.oauth_key == "sk-ant-oat-test"
    assert loaded.providers.openai.api_key == "sk-openai-test"
    assert loaded.services.web_search.api_key == "brave-key"
    assert loaded.channels.telegram.bot_token == "bot123:ABC"


def test_file_permissions(tmp_path):
    """Saved credentials file has 0o600 permissions."""
    path = tmp_path / "credentials.json"
    save_credentials(Credentials(), path)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_camel_case_serialization(tmp_path):
    """Credentials are serialized with camelCase keys."""
    path = tmp_path / "credentials.json"

    creds = Credentials()
    creds.providers.anthropic.oauth_key = "tok"
    creds.services.web_search.api_key = "key"
    creds.channels.telegram.bot_token = "bot"

    save_credentials(creds, path)

    with open(path) as f:
        raw = json.load(f)

    # Top-level keys should be camelCase
    assert "providers" in raw
    assert "oauthKey" in raw["providers"]["anthropic"]
    assert "webSearch" in raw["services"]
    assert "botToken" in raw["channels"]["telegram"]


def test_load_missing_file_returns_defaults(tmp_path):
    """Loading from a non-existent path returns defaults."""
    path = tmp_path / "nope.json"
    creds = load_credentials(path)
    assert creds == Credentials()


def test_load_malformed_json_returns_defaults(tmp_path):
    """Loading from a malformed file returns defaults."""
    path = tmp_path / "credentials.json"
    path.write_text("not json {{{")
    creds = load_credentials(path)
    assert creds == Credentials()
