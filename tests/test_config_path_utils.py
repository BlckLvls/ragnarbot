"""Tests for config path_utils module."""

import pytest

from ragnarbot.config.path_utils import (
    get_all_paths,
    get_by_path,
    get_field_meta,
    resolve_field_name,
    set_by_path,
)
from ragnarbot.config.schema import AgentDefaults, Config


def test_resolve_field_name_snake_case():
    assert resolve_field_name(AgentDefaults, "max_context_tokens") == "max_context_tokens"


def test_resolve_field_name_camel_case():
    assert resolve_field_name(AgentDefaults, "maxContextTokens") == "max_context_tokens"


def test_resolve_field_name_exact_match():
    assert resolve_field_name(AgentDefaults, "stream_steps") == "stream_steps"


def test_resolve_field_name_not_found():
    assert resolve_field_name(AgentDefaults, "nonexistent") is None


def test_get_by_path_nested():
    config = Config()
    assert get_by_path(config, "agents.defaults.debounce_seconds") == 0.5


def test_get_by_path_deep():
    config = Config()
    assert get_by_path(config, "tools.web.search.engine") == "brave"


def test_get_by_path_top_level_model():
    config = Config()
    # Should return the sub-model
    result = get_by_path(config, "agents.defaults.model")
    assert "anthropic" in result


def test_get_by_path_invalid():
    config = Config()
    with pytest.raises(ValueError, match="Unknown field"):
        get_by_path(config, "agents.defaults.nonexistent")


def test_get_by_path_camel_case():
    config = Config()
    assert get_by_path(config, "agents.defaults.maxContextTokens") == 200_000


def test_set_by_path_float_coercion():
    config = Config()
    set_by_path(config, "agents.defaults.debounce_seconds", "1.5")
    assert config.agents.defaults.debounce_seconds == 1.5


def test_set_by_path_int_coercion():
    config = Config()
    set_by_path(config, "agents.defaults.max_context_tokens", "100000")
    assert config.agents.defaults.max_context_tokens == 100000


def test_set_by_path_bool_coercion():
    config = Config()
    set_by_path(config, "agents.defaults.stream_steps", "false")
    assert config.agents.defaults.stream_steps is False


def test_set_by_path_bool_coercion_true():
    config = Config()
    config.agents.defaults.stream_steps = False
    set_by_path(config, "agents.defaults.stream_steps", "true")
    assert config.agents.defaults.stream_steps is True


def test_set_by_path_validation_failure():
    config = Config()
    with pytest.raises(ValueError, match="Validation failed"):
        set_by_path(config, "agents.defaults.context_mode", "invalid_mode")


def test_set_by_path_camel_case():
    config = Config()
    set_by_path(config, "agents.defaults.debounceSeconds", "1.0")
    assert config.agents.defaults.debounce_seconds == 1.0


def test_get_all_paths_flattening():
    config = Config()
    paths = get_all_paths(config)
    assert "agents.defaults.debounce_seconds" in paths
    assert "tools.web.search.engine" in paths
    assert "gateway.port" in paths
    # Should not contain sub-model keys
    assert "agents.defaults" not in paths
    assert "agents" not in paths


def test_get_all_paths_values():
    config = Config()
    paths = get_all_paths(config)
    assert paths["agents.defaults.debounce_seconds"] == 0.5
    assert paths["gateway.port"] == 18790


def test_get_field_meta_returns_reload():
    meta = get_field_meta(Config, "agents.defaults.debounce_seconds")
    assert meta["reload"] == "hot"
    assert meta["type"] == "float"
    assert "label" in meta


def test_get_field_meta_warm():
    meta = get_field_meta(Config, "agents.defaults.model")
    assert meta["reload"] == "warm"


def test_get_field_meta_cold():
    meta = get_field_meta(Config, "agents.defaults.workspace")
    assert meta["reload"] == "cold"


def test_get_field_meta_pattern():
    meta = get_field_meta(Config, "agents.defaults.context_mode")
    assert "pattern" in meta
    assert "eco" in meta["pattern"]


def test_get_field_meta_invalid_path():
    with pytest.raises(ValueError, match="Unknown field"):
        get_field_meta(Config, "nonexistent.path")
