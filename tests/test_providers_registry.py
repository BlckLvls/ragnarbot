"""Tests for provider models registry."""

from ragnarbot.config.providers import PROVIDERS, get_models, get_provider, supports_oauth


def test_providers_has_expected_entries():
    assert len(PROVIDERS) == 4
    ids = [p["id"] for p in PROVIDERS]
    assert ids == ["anthropic", "openai", "gemini", "openrouter"]


def test_each_provider_has_required_fields():
    for p in PROVIDERS:
        assert "id" in p
        assert "name" in p
        assert "description" in p
        assert "api_key_url" in p
        assert "models" in p
        assert len(p["models"]) >= 1


def test_each_model_has_required_fields():
    for p in PROVIDERS:
        for m in p["models"]:
            assert "id" in m
            assert "name" in m
            assert "description" in m
            assert "/" in m["id"], f"Model ID should have provider prefix: {m['id']}"


def test_model_ids_match_provider():
    for p in PROVIDERS:
        for m in p["models"]:
            prefix = m["id"].split("/")[0]
            assert prefix == p["id"], f"Model {m['id']} prefix doesn't match provider {p['id']}"


def test_get_provider_found():
    p = get_provider("anthropic")
    assert p is not None
    assert p["name"] == "Anthropic"


def test_get_provider_not_found():
    assert get_provider("nonexistent") is None


def test_get_models_returns_list():
    models = get_models("anthropic")
    assert len(models) == 2
    assert models[0]["id"] == "anthropic/claude-opus-4-6"


def test_anthropic_registry_drops_4_5_models():
    model_ids = [m["id"] for m in get_models("anthropic")]
    assert "anthropic/claude-sonnet-4-5" not in model_ids
    assert "anthropic/claude-haiku-4-5" not in model_ids


def test_openai_models_include_gpt_5_4_first():
    models = get_models("openai")
    model_ids = [m["id"] for m in models]

    assert models[0]["id"] == "openai/gpt-5.4"
    assert "openai/gpt-5.2" in model_ids
    assert "openai/gpt-5-mini" in model_ids


def test_openrouter_models_include_gpt_5_4():
    models = get_models("openrouter")
    model_ids = [m["id"] for m in models]

    assert "openrouter/openai/gpt-5.4" in model_ids
    assert "openrouter/openai/gpt-5.2" in model_ids


def test_gemini_models_include_3_1_pro_first():
    models = get_models("gemini")
    model_ids = [m["id"] for m in models]

    assert models[0]["id"] == "gemini/gemini-3.1-pro-preview"
    assert "gemini/gemini-3-pro-preview" in model_ids
    assert "gemini/gemini-3-flash-preview" in model_ids


def test_openrouter_models_include_gemini_3_1_pro():
    models = get_models("openrouter")
    model_ids = [m["id"] for m in models]

    assert "openrouter/google/gemini-3.1-pro-preview" in model_ids
    assert "openrouter/google/gemini-3-pro-preview" in model_ids


def test_get_models_empty_for_unknown():
    assert get_models("nonexistent") == []


def test_supports_oauth_anthropic():
    assert supports_oauth("anthropic") is True


def test_supports_oauth_others():
    assert supports_oauth("openai") is True
    assert supports_oauth("gemini") is True
    assert supports_oauth("openrouter") is False
