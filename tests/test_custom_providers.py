"""Custom OpenAI-compatible server support: registry, validation, web API."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from ragnarbot.auth.credentials import load_credentials, save_credentials
from ragnarbot.config.loader import load_config, save_config
from ragnarbot.config.providers import (
    custom_model_id,
    custom_provider_secret_name,
    model_supports_vision,
    resolve_custom_model,
    split_custom_model,
)
from ragnarbot.config.schema import Config, CustomModelConfig, CustomProviderConfig
from ragnarbot.config.validation import validate_model_auth
from ragnarbot.web.api import ApiRoutes

QWEN = "dist/qwen3_6-35B-A3B-q4f16_1"


def make_config(**model_kwargs) -> Config:
    config = Config()
    config.custom_providers.append(CustomProviderConfig(
        id="jetson",
        name="Jetson MLC",
        base_url="http://127.0.0.1:8000/v1",
        models=[CustomModelConfig(id=QWEN, **model_kwargs)],
    ))
    return config


# ── registry ─────────────────────────────────────────────────────

def test_split_custom_model():
    assert split_custom_model(f"custom/jetson/{QWEN}") == ("jetson", QWEN)
    assert split_custom_model("custom/jetson") is None
    assert split_custom_model("anthropic/claude-opus-4-8") is None


def test_resolve_custom_model():
    config = make_config()
    resolved = resolve_custom_model(custom_model_id("jetson", QWEN), config)
    assert resolved is not None
    assert resolved.base_url == "http://127.0.0.1:8000/v1"
    assert resolved.model_id == QWEN
    assert resolved.vision is False

    assert resolve_custom_model("custom/unknown/model", config) is None
    # Undeclared model on a known server still resolves (discovered on the fly)
    assert resolve_custom_model("custom/jetson/other-model", config) is not None


def test_custom_model_vision_flag(monkeypatch):
    config = make_config(vision=True)
    monkeypatch.setattr("ragnarbot.config.loader.load_config", lambda *a, **k: config)
    assert model_supports_vision(f"custom/jetson/{QWEN}") is True

    config_no_vision = make_config()
    monkeypatch.setattr("ragnarbot.config.loader.load_config", lambda *a, **k: config_no_vision)
    assert model_supports_vision(f"custom/jetson/{QWEN}") is False


# ── validation ───────────────────────────────────────────────────

def test_validate_model_auth_custom(monkeypatch):
    config = make_config()
    monkeypatch.setattr("ragnarbot.config.loader.load_config", lambda *a, **k: config)

    assert validate_model_auth(f"custom/jetson/{QWEN}", "api_key") is None
    assert "OAuth" in validate_model_auth(f"custom/jetson/{QWEN}", "oauth")
    assert "Unknown custom server" in validate_model_auth("custom/nope/model", "api_key")

    config.custom_providers[0].base_url = ""
    assert "no base URL" in validate_model_auth(f"custom/jetson/{QWEN}", "api_key")


# ── web API ──────────────────────────────────────────────────────

class JsonRequest(SimpleNamespace):
    async def json(self):
        return self.body

    async def text(self):
        return json.dumps(self.body) if self.body else ""


def make_routes() -> ApiRoutes:
    agent = SimpleNamespace(tools=SimpleNamespace(get=lambda name: None))
    server = SimpleNamespace(agent=agent, config=SimpleNamespace(), notifications=None)
    return ApiRoutes(server)


@pytest.mark.asyncio
async def test_custom_server_crud_roundtrip():
    routes = make_routes()

    resp = await routes.models_custom_add(JsonRequest(body={
        "name": "Jetson MLC",
        "base_url": "http://127.0.0.1:8000/v1/",
        "api_key": "sekret",
        "models": [QWEN, {"id": "other", "vision": True}],
    }))
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    server_id = payload["id"]

    config = load_config()
    server = config.custom_providers[0]
    assert server.id == server_id
    assert server.base_url == "http://127.0.0.1:8000/v1"  # trailing slash stripped
    assert [m.id for m in server.models] == [QWEN, "other"]
    assert server.models[1].vision is True
    creds = load_credentials()
    assert creds.extra[custom_provider_secret_name(server_id)] == "sekret"

    overview = await routes.models_overview(SimpleNamespace())
    data = json.loads(overview.text)
    assert data["custom"][0]["api_key_set"] is True
    assert data["custom"][0]["models"][0]["full_id"] == custom_model_id(server_id, QWEN)
    assert any(p["id"] == "anthropic" for p in data["providers"])

    resp = await routes.models_custom_update(JsonRequest(
        body={"name": "Renamed", "api_key": ""},
        match_info={"server_id": server_id},
    ))
    assert json.loads(resp.text)["ok"] is True
    assert load_config().custom_providers[0].name == "Renamed"
    assert custom_provider_secret_name(server_id) not in load_credentials().extra

    resp = await routes.models_custom_delete(JsonRequest(match_info={"server_id": server_id}))
    assert json.loads(resp.text)["ok"] is True
    assert load_config().custom_providers == []


@pytest.mark.asyncio
async def test_custom_server_add_rejects_bad_input():
    routes = make_routes()
    resp = await routes.models_custom_add(JsonRequest(body={"name": "x", "base_url": "ftp://nope"}))
    assert resp.status == 400
    resp = await routes.models_custom_add(JsonRequest(body={"base_url": "http://ok:1/v1"}))
    assert resp.status == 400  # no usable id/name


@pytest.mark.asyncio
async def test_custom_server_add_duplicate_conflicts():
    routes = make_routes()
    body = {"id": "jetson", "base_url": "http://127.0.0.1:8000/v1"}
    await routes.models_custom_add(JsonRequest(body=body))
    resp = await routes.models_custom_add(JsonRequest(body=body))
    assert resp.status == 409


@pytest.mark.asyncio
async def test_custom_server_delete_blocked_while_in_use():
    routes = make_routes()
    await routes.models_custom_add(JsonRequest(body={
        "id": "jetson", "base_url": "http://127.0.0.1:8000/v1", "models": [QWEN],
    }))
    config = load_config()
    config.agents.defaults.model = custom_model_id("jetson", QWEN)
    save_config(config)

    resp = await routes.models_custom_delete(JsonRequest(match_info={"server_id": "jetson"}))
    assert resp.status == 409
    assert load_config().custom_providers  # still there


@pytest.mark.asyncio
async def test_probe_unknown_server_404():
    routes = make_routes()
    with pytest.raises(web.HTTPNotFound):
        await routes.models_custom_probe(JsonRequest(body={}, match_info={"server_id": "nope"}))


@pytest.mark.asyncio
async def test_probe_unreachable_server_reports_error():
    routes = make_routes()
    await routes.models_custom_add(JsonRequest(body={
        "id": "dead", "base_url": "http://127.0.0.1:9/v1",
    }))
    resp = await routes.models_custom_probe(JsonRequest(body={}, match_info={"server_id": "dead"}))
    payload = json.loads(resp.text)
    assert payload["ok"] is False
    assert payload["error"]


# ── model selection ──────────────────────────────────────────────

async def _add_jetson(routes):
    await routes.models_custom_add(JsonRequest(body={
        "id": "jetson", "base_url": "http://127.0.0.1:8000/v1", "models": [QWEN],
    }))


@pytest.mark.asyncio
async def test_select_custom_model_switches_auth_to_api_key():
    routes = make_routes()
    await _add_jetson(routes)
    config = load_config()
    config.agents.defaults.auth_method = "oauth"
    save_config(config)

    resp = await routes.models_select(JsonRequest(body={
        "model": custom_model_id("jetson", QWEN), "target": "primary",
    }))
    payload = json.loads(resp.text)
    # No switch_model on the stub agent → hot reload unavailable → restart path.
    assert payload["restart_required"] is True
    assert payload["auth_method"] == "api_key"

    config = load_config()
    assert config.agents.defaults.model == custom_model_id("jetson", QWEN)
    assert config.agents.defaults.auth_method == "api_key"


@pytest.mark.asyncio
async def test_select_hot_applies_when_agent_supports_switch():
    calls = []
    routes = make_routes()
    routes.agent.switch_model = lambda model, auth: calls.append((model, auth)) and None
    await _add_jetson(routes)

    resp = await routes.models_select(JsonRequest(body={
        "model": custom_model_id("jetson", QWEN), "target": "primary",
    }))
    payload = json.loads(resp.text)
    assert payload["restart_required"] is False
    assert payload["status"] == "applied"
    assert calls == [(custom_model_id("jetson", QWEN), "api_key")]


@pytest.mark.asyncio
async def test_select_rejects_model_without_credentials():
    routes = make_routes()
    resp = await routes.models_select(JsonRequest(body={
        "model": "openrouter/qwen/qwen3.5-plus-02-15", "target": "primary",
    }))
    assert resp.status == 400  # no openrouter key configured in the isolated profile


@pytest.mark.asyncio
async def test_select_fallback_set_and_clear():
    routes = make_routes()
    await _add_jetson(routes)

    resp = await routes.models_select(JsonRequest(body={
        "model": custom_model_id("jetson", QWEN), "target": "fallback",
    }))
    assert json.loads(resp.text)["status"] == "applied"
    config = load_config()
    assert config.agents.fallback.model == custom_model_id("jetson", QWEN)
    assert config.agents.fallback.auth_method == "api_key"

    resp = await routes.models_select(JsonRequest(body={"model": "", "target": "fallback"}))
    assert json.loads(resp.text)["detail"] == "Fallback model cleared."
    assert load_config().agents.fallback.model is None


# ── credentials survive round-trip ───────────────────────────────

def test_custom_api_key_persists_in_extra():
    creds = load_credentials()
    creds.extra[custom_provider_secret_name("jetson")] = "abc"
    save_credentials(creds)
    assert load_credentials().extra["custom_jetson_api_key"] == "abc"
