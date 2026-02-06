"""Tests for config-to-credentials migration."""

import json

from ragnarbot.config.loader import migrate_credentials_from_config, convert_keys


def test_migrate_full_config(tmp_path):
    """Old config with all credentials migrates correctly."""
    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "credentials.json"

    old_config = {
        "providers": {
            "anthropic": {"apiKey": "sk-ant-test", "apiBase": None},
            "openai": {"apiKey": "sk-openai-test"},
            "gemini": {"apiKey": ""},
        },
        "transcription": {"apiKey": "groq-key"},
        "tools": {
            "web": {"search": {"apiKey": "brave-key", "maxResults": 5}},
            "exec": {"timeout": 60},
        },
        "channels": {
            "telegram": {"enabled": True, "token": "bot123:ABC", "allowFrom": []},
        },
        "agents": {"defaults": {"model": "anthropic/claude-opus-4-5"}},
    }

    config_path.write_text(json.dumps(old_config))

    migrate_credentials_from_config(config_path, creds_path)

    # Credentials file should exist with correct values
    assert creds_path.exists()
    with open(creds_path) as f:
        creds_raw = json.load(f)
    creds = convert_keys(creds_raw)

    assert creds["providers"]["anthropic"]["api_key"] == "sk-ant-test"
    assert creds["providers"]["anthropic"]["auth_method"] == "api_key"
    assert creds["providers"]["openai"]["api_key"] == "sk-openai-test"
    assert creds["services"]["transcription"]["api_key"] == "groq-key"
    assert creds["services"]["web_search"]["api_key"] == "brave-key"
    assert creds["channels"]["telegram"]["bot_token"] == "bot123:ABC"

    # Config file should have credentials stripped
    with open(config_path) as f:
        updated_config = json.load(f)

    assert "apiKey" not in updated_config["providers"]["anthropic"]
    assert "apiKey" not in updated_config["providers"]["openai"]
    assert "transcription" not in updated_config
    assert "apiKey" not in updated_config["tools"]["web"]["search"]
    assert "token" not in updated_config["channels"]["telegram"]

    # Non-credential fields should be preserved
    assert updated_config["channels"]["telegram"]["enabled"] is True
    assert updated_config["tools"]["web"]["search"]["maxResults"] == 5
    assert updated_config["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-5"


def test_migrate_empty_config(tmp_path):
    """Config with no credentials doesn't create credentials file."""
    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "credentials.json"

    config_path.write_text(json.dumps({
        "providers": {
            "anthropic": {"apiKey": ""},
            "openai": {"apiKey": ""},
        },
        "agents": {"defaults": {"model": "anthropic/claude-opus-4-5"}},
    }))

    migrate_credentials_from_config(config_path, creds_path)

    # No credentials to migrate, so file should not be created
    assert not creds_path.exists()


def test_migrate_partial_config(tmp_path):
    """Config with only some credentials migrates those."""
    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "credentials.json"

    config_path.write_text(json.dumps({
        "providers": {
            "anthropic": {"apiKey": "sk-ant-only"},
        },
    }))

    migrate_credentials_from_config(config_path, creds_path)

    assert creds_path.exists()
    with open(creds_path) as f:
        creds_raw = json.load(f)
    creds = convert_keys(creds_raw)
    assert creds["providers"]["anthropic"]["api_key"] == "sk-ant-only"


def test_migrate_invalid_config(tmp_path):
    """Invalid JSON config doesn't crash."""
    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "credentials.json"
    config_path.write_text("not valid json {{{")

    # Should not raise
    migrate_credentials_from_config(config_path, creds_path)
    assert not creds_path.exists()
