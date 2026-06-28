"""Tests for the tools.search config block."""

from ragnarbot.config.loader import convert_keys, convert_to_camel
from ragnarbot.config.schema import Config, SearchToolConfig


def test_search_defaults():
    cfg = Config()
    assert cfg.tools.search.backend == "auto"
    assert cfg.tools.search.auto_install is True
    assert cfg.tools.search.max_matches == 200
    assert cfg.tools.search.max_results == 200
    assert cfg.tools.search.max_output_chars == 20000
    assert cfg.tools.search.timeout == 30


def test_search_camel_serialization():
    camel = convert_to_camel(Config().model_dump())
    search = camel["tools"]["search"]
    assert "maxMatches" in search
    assert "maxOutputChars" in search
    assert "backend" in search


def test_search_camel_roundtrip_load():
    raw = {"tools": {"search": {"backend": "python", "maxMatches": 50, "maxOutputChars": 5000}}}
    cfg = Config(**convert_keys(raw))
    assert cfg.tools.search.backend == "python"
    assert cfg.tools.search.max_matches == 50
    assert cfg.tools.search.max_output_chars == 5000
    # untouched fields keep defaults
    assert cfg.tools.search.max_results == 200


def test_search_backend_validation():
    import pytest
    from pydantic import ValidationError

    SearchToolConfig(backend="ripgrep")  # valid
    with pytest.raises(ValidationError):
        SearchToolConfig(backend="bogus")
