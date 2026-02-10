"""Tests for config schema migration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ragnarbot.config.migration import (
    MigrationResult,
    _deep_diff,
    _delete_nested,
    _has_meaningful_data,
    _is_sensitive,
    _mask_value,
    _set_nested,
    migrate_config,
    migrate_credentials,
    run_startup_migration,
)


class TestDeepDiff:
    def test_no_diff(self):
        a = {"x": 1, "y": 2}
        b = {"x": 1, "y": 2}
        added, removed = _deep_diff(a, b)
        assert added == {}
        assert removed == {}

    def test_added_key(self):
        existing = {"x": 1}
        default = {"x": 1, "y": 2}
        added, removed = _deep_diff(existing, default)
        assert added == {"y": 2}
        assert removed == {}

    def test_removed_key(self):
        existing = {"x": 1, "old": "val"}
        default = {"x": 1}
        added, removed = _deep_diff(existing, default)
        assert added == {}
        assert removed == {"old": "val"}

    def test_nested_diff(self):
        existing = {"a": {"b": 1}}
        default = {"a": {"b": 1, "c": 2}}
        added, removed = _deep_diff(existing, default)
        assert added == {"a.c": 2}
        assert removed == {}

    def test_nested_removal(self):
        existing = {"a": {"b": 1, "old": "x"}}
        default = {"a": {"b": 1}}
        added, removed = _deep_diff(existing, default)
        assert removed == {"a.old": "x"}


class TestHelpers:
    def test_meaningful_data_string(self):
        assert _has_meaningful_data("hello") is True
        assert _has_meaningful_data("") is False

    def test_meaningful_data_dict(self):
        assert _has_meaningful_data({"key": "val"}) is True
        assert _has_meaningful_data({"key": ""}) is False

    def test_meaningful_data_none(self):
        assert _has_meaningful_data(None) is False

    def test_meaningful_data_list(self):
        assert _has_meaningful_data([1, 2]) is True
        assert _has_meaningful_data([]) is False

    def test_is_sensitive(self):
        assert _is_sensitive("providers.anthropic.api_key") is True
        assert _is_sensitive("providers.openai.oauth_key") is True
        assert _is_sensitive("channels.telegram.bot_token") is True
        assert _is_sensitive("agents.defaults.model") is False

    def test_mask_value_long_string(self):
        assert _mask_value("sk-test-long-key-123") == "sk-t****23"

    def test_mask_value_short_string(self):
        assert _mask_value("short") == "'short'"

    def test_set_nested(self):
        d = {"a": {"b": 1}}
        _set_nested(d, "a.c", 2)
        assert d == {"a": {"b": 1, "c": 2}}

    def test_set_nested_creates_parents(self):
        d = {}
        _set_nested(d, "a.b.c", 1)
        assert d == {"a": {"b": {"c": 1}}}

    def test_delete_nested(self):
        d = {"a": {"b": 1, "c": 2}}
        _delete_nested(d, "a.c")
        assert d == {"a": {"b": 1}}

    def test_delete_nested_missing(self):
        d = {"a": 1}
        _delete_nested(d, "b.c")  # should not raise
        assert d == {"a": 1}


class TestMigrateConfig:
    def test_adds_missing_fields(self, tmp_path):
        config = {"agents": {"defaults": {"model": "anthropic/claude-opus-4-6"}}}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))

        result = migrate_config(path)
        assert "gateway" in result.data
        assert "channels" in result.data
        assert "tools" in result.data
        assert result.added  # should have added fields

    def test_preserves_existing_values(self, tmp_path):
        config = {
            "agents": {
                "defaults": {
                    "model": "openai/gpt-5.2",
                    "maxTokens": 4096,
                }
            }
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))

        result = migrate_config(path)
        assert result.data["agents"]["defaults"]["model"] == "openai/gpt-5.2"
        assert result.data["agents"]["defaults"]["max_tokens"] == 4096

    def test_removes_unknown_empty(self, tmp_path):
        config = {
            "agents": {"defaults": {"model": "anthropic/claude-opus-4-6", "oldField": ""}},
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))

        result = migrate_config(path)
        assert "old_field" not in result.data.get("agents", {}).get("defaults", {})
        assert "agents.defaults.old_field" in result.auto_removed

    def test_meaningful_removal_goes_to_needs_confirm(self, tmp_path):
        config = {
            "agents": {"defaults": {"model": "anthropic/claude-opus-4-6", "oldField": "data"}},
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))

        result = migrate_config(path)
        assert "agents.defaults.old_field" in result.needs_confirm
        # The field should still be in data (not yet removed)
        assert result.data["agents"]["defaults"]["old_field"] == "data"

    def test_no_changes_returns_empty_result(self, tmp_path):
        from ragnarbot.config.schema import Config
        from ragnarbot.config.loader import convert_to_camel

        config = Config()
        path = tmp_path / "config.json"
        path.write_text(json.dumps(convert_to_camel(config.model_dump()), indent=2))

        result = migrate_config(path)
        assert not result.has_changes


class TestMigrateCredentials:
    def test_adds_missing_sections(self, tmp_path):
        creds = {"providers": {"anthropic": {"apiKey": "sk-test"}}}
        path = tmp_path / "creds.json"
        path.write_text(json.dumps(creds))

        result = migrate_credentials(path)
        assert "services" in result.data
        assert "channels" in result.data

    def test_preserves_existing_keys(self, tmp_path):
        creds = {
            "providers": {
                "anthropic": {"apiKey": "sk-test-123"},
                "openai": {"apiKey": ""},
                "gemini": {"apiKey": ""},
            },
            "services": {"groq": {"apiKey": ""}, "elevenlabs": {"apiKey": ""}, "braveSearch": {"apiKey": ""}},
            "channels": {"telegram": {"botToken": ""}},
        }
        path = tmp_path / "creds.json"
        path.write_text(json.dumps(creds))

        result = migrate_credentials(path)
        assert result.data["providers"]["anthropic"]["api_key"] == "sk-test-123"

    def test_meaningful_removal_goes_to_needs_confirm(self, tmp_path):
        creds = {
            "providers": {
                "anthropic": {"apiKey": "sk-test"},
                "openai": {"apiKey": ""},
                "gemini": {"apiKey": ""},
            },
            "services": {"groq": {"apiKey": ""}, "elevenlabs": {"apiKey": ""}, "braveSearch": {"apiKey": ""}},
            "channels": {"telegram": {"botToken": ""}},
            "oldSection": {"key": "important-data"},
        }
        path = tmp_path / "creds.json"
        path.write_text(json.dumps(creds))

        result = migrate_credentials(path)
        assert "old_section" in result.needs_confirm


class TestRunStartupMigration:
    def test_no_files_returns_true(self, tmp_path):
        from rich.console import Console

        console = Console()
        with (
            patch("ragnarbot.config.migration.get_config_path",
                  return_value=tmp_path / "missing.json"),
            patch("ragnarbot.config.migration.get_credentials_path",
                  return_value=tmp_path / "missing_creds.json"),
        ):
            assert run_startup_migration(console) is True

    def test_no_changes_returns_true(self, tmp_path):
        from ragnarbot.config.loader import convert_to_camel
        from ragnarbot.config.schema import Config
        from ragnarbot.auth.credentials import Credentials
        from rich.console import Console

        console = Console()

        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(convert_to_camel(Config().model_dump()), indent=2)
        )

        creds_path = tmp_path / "creds.json"
        creds_path.write_text(
            json.dumps(convert_to_camel(Credentials().model_dump()), indent=2)
        )

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
        ):
            assert run_startup_migration(console) is True

    def test_auto_adds_missing_fields(self, tmp_path):
        from rich.console import Console

        console = Console()

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(
            {"agents": {"defaults": {"model": "anthropic/claude-opus-4-6"}}}
        ))

        creds_path = tmp_path / "missing_creds.json"

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
        ):
            assert run_startup_migration(console) is True

        # Config should have been saved with new fields
        saved = json.loads(config_path.read_text())
        assert "gateway" in saved or "channels" in saved

    def test_auto_removes_empty_fields(self, tmp_path):
        from ragnarbot.config.loader import convert_to_camel
        from ragnarbot.config.schema import Config
        from rich.console import Console

        console = Console()

        data = convert_to_camel(Config().model_dump())
        data["oldEmptyField"] = ""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(data, indent=2))

        creds_path = tmp_path / "missing_creds.json"

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
        ):
            assert run_startup_migration(console) is True

        saved = json.loads(config_path.read_text())
        assert "oldEmptyField" not in saved

    def test_prompts_for_meaningful_removals_accepted(self, tmp_path):
        from ragnarbot.config.loader import convert_to_camel
        from ragnarbot.config.schema import Config
        from rich.console import Console

        console = Console()

        data = convert_to_camel(Config().model_dump())
        data["obsoleteField"] = "important-data"
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(data, indent=2))

        creds_path = tmp_path / "missing_creds.json"

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
            patch("ragnarbot.config.migration.typer.confirm", return_value=True),
        ):
            assert run_startup_migration(console) is True

        saved = json.loads(config_path.read_text())
        assert "obsoleteField" not in saved

    def test_prompts_for_meaningful_removals_declined(self, tmp_path):
        from ragnarbot.config.loader import convert_to_camel
        from ragnarbot.config.schema import Config
        from rich.console import Console

        console = Console()

        data = convert_to_camel(Config().model_dump())
        data["obsoleteField"] = "important-data"
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(data, indent=2))

        creds_path = tmp_path / "missing_creds.json"

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
            patch("ragnarbot.config.migration.typer.confirm", return_value=False),
        ):
            assert run_startup_migration(console) is False

    def test_credentials_migration_alongside_config(self, tmp_path):
        from ragnarbot.config.loader import convert_to_camel
        from ragnarbot.config.schema import Config
        from ragnarbot.auth.credentials import Credentials
        from rich.console import Console

        console = Console()

        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(convert_to_camel(Config().model_dump()), indent=2)
        )

        creds_data = convert_to_camel(Credentials().model_dump())
        creds_data["oldSection"] = ""
        creds_path = tmp_path / "creds.json"
        creds_path.write_text(json.dumps(creds_data, indent=2))

        with (
            patch("ragnarbot.config.migration.get_config_path", return_value=config_path),
            patch("ragnarbot.config.migration.get_credentials_path", return_value=creds_path),
        ):
            assert run_startup_migration(console) is True

        saved = json.loads(creds_path.read_text())
        assert "oldSection" not in saved
