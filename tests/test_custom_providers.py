"""Custom OpenAI-compatible server support: registry, validation, web API."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from ragnarbot.agent.loop import AgentLoop
from ragnarbot.agent.tools.config_tool import ConfigTool
from ragnarbot.auth.credentials import load_credentials, save_credentials
from ragnarbot.config.loader import load_config, save_config
from ragnarbot.config.providers import (
    custom_model_id,
    custom_provider_secret_name,
    model_supports_vision,
    resolve_custom_model,
    split_custom_model,
)
from ragnarbot.config.schema import (
    Config,
    CustomModelConfig,
    CustomProviderConfig,
    ExecToolConfig,
)
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
    broadcasts = []
    routes = make_routes()
    routes.agent.switch_model = lambda model, auth: calls.append((model, auth)) and None

    class Channel:
        async def broadcast(self, event):
            broadcasts.append(event)

    routes.server.channel = Channel()
    routes.server._build_state = lambda: {"model": custom_model_id("jetson", QWEN)}
    await _add_jetson(routes)

    resp = await routes.models_select(JsonRequest(body={
        "model": custom_model_id("jetson", QWEN), "target": "primary",
    }))
    payload = json.loads(resp.text)
    assert payload["restart_required"] is False
    assert payload["status"] == "applied"
    assert calls == [(custom_model_id("jetson", QWEN), "api_key")]
    # Open web chats got a fresh state event so the header updates live.
    assert broadcasts == [{"type": "state", "model": custom_model_id("jetson", QWEN)}]


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
    assert load_credentials().extra["custom-jetson-api-key"] == "abc"


def test_custom_api_key_round_trips_for_awkward_ids():
    """Ids with digit/letter boundaries or underscores must survive save/load.

    The camelCase<->snake_case conversion in credentials persistence is lossy
    for keys like custom_gpu2x_api_key — the hyphenated secret name sidesteps
    it entirely.
    """
    creds = load_credentials()
    for server_id in ("gpu2x", "llama_3", "mistral_7b", "srv_"):
        creds.extra[custom_provider_secret_name(server_id)] = f"key-{server_id}"
    save_credentials(creds)
    reloaded = load_credentials()
    for server_id in ("gpu2x", "llama_3", "mistral_7b", "srv_"):
        assert reloaded.extra[custom_provider_secret_name(server_id)] == f"key-{server_id}"


def test_custom_secret_names_do_not_collide():
    """Ids differing only in underscores must map to distinct secret names."""
    ids = ("ab", "ab_", "a_b", "a__b")
    names = {custom_provider_secret_name(i) for i in ids}
    assert len(names) == len(ids)


# ── probe happy-path (HTTP mocked) ───────────────────────────────

class _FakeProbeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, *, status=200, json_value=None, json_error=None):
        self.status = status
        self._json_value = json_value
        self._json_error = json_error

    async def json(self, content_type=None):
        if self._json_error is not None:
            raise self._json_error
        return self._json_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeProbeSession:
    """Stand-in for aiohttp.ClientSession that hands back a fixed response."""

    def __init__(self, response):
        self._response = response

    def get(self, url, headers=None):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_probe_http(monkeypatch, response):
    """Replace aiohttp.ClientSession so the probe never touches the network."""
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda *a, **k: _FakeProbeSession(response)
    )


@pytest.mark.asyncio
async def test_probe_reports_models_without_saving(monkeypatch):
    """Probe returns discovered ids and leaves the config untouched by default."""
    routes = make_routes()
    await _add_jetson(routes)
    _patch_probe_http(monkeypatch, _FakeProbeResponse(
        json_value={"data": [{"id": "m1"}, {"id": "m2"}]},
    ))

    resp = await routes.models_custom_probe(
        JsonRequest(body={}, match_info={"server_id": "jetson"})
    )
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["models"] == ["m1", "m2"]
    assert payload["added"] == []
    # Nothing persisted — the server still lists only its seed model.
    assert [m.id for m in load_config().custom_providers[0].models] == [QWEN]


@pytest.mark.asyncio
async def test_probe_save_merges_new_models_without_duplicates(monkeypatch):
    """save=True appends only unknown ids and persists them."""
    routes = make_routes()
    await routes.models_custom_add(JsonRequest(body={
        "id": "jetson", "base_url": "http://127.0.0.1:8000/v1",
        "models": [QWEN, "m1"],
    }))
    _patch_probe_http(monkeypatch, _FakeProbeResponse(
        json_value={"data": [{"id": "m1"}, {"id": "m2"}]},
    ))

    resp = await routes.models_custom_probe(
        JsonRequest(body={"save": True}, match_info={"server_id": "jetson"})
    )
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["models"] == ["m1", "m2"]
    assert payload["added"] == ["m2"]  # m1 already known, not duplicated
    reloaded = load_config().custom_providers[0]
    assert [m.id for m in reloaded.models] == [QWEN, "m1", "m2"]


@pytest.mark.asyncio
async def test_probe_rejects_non_list_shape(monkeypatch):
    """A non-list `data` field yields an explicit shape error."""
    routes = make_routes()
    await _add_jetson(routes)
    _patch_probe_http(monkeypatch, _FakeProbeResponse(json_value={"data": "oops"}))

    resp = await routes.models_custom_probe(
        JsonRequest(body={}, match_info={"server_id": "jetson"})
    )
    payload = json.loads(resp.text)
    assert payload["ok"] is False
    assert "unexpected /models response shape" in payload["error"]


@pytest.mark.asyncio
async def test_probe_reports_error_when_body_not_json(monkeypatch):
    """A response body that isn't JSON surfaces as ok=False with an error."""
    routes = make_routes()
    await _add_jetson(routes)
    _patch_probe_http(monkeypatch, _FakeProbeResponse(
        json_error=ValueError("Attempt to decode JSON with unexpected mimetype"),
    ))

    resp = await routes.models_custom_probe(
        JsonRequest(body={}, match_info={"server_id": "jetson"})
    )
    payload = json.loads(resp.text)
    assert payload["ok"] is False
    assert payload["error"]


# ── AgentLoop.switch_model hot swap ──────────────────────────────

def _make_switchable_agent(tmp_path):
    """Build a real AgentLoop wired to a factory that returns a fresh provider."""
    old_model = "openai/gpt-5.6-sol"
    primary = MagicMock()
    primary.get_default_model.return_value = old_model
    new_provider = MagicMock(name="switched-provider")
    provider_factory = MagicMock(return_value=new_provider)

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    agent = AgentLoop(
        bus=bus,
        provider=primary,
        workspace=workspace,
        model=old_model,
        exec_config=ExecToolConfig(),
        auth_method="oauth",
        provider_factory=provider_factory,
    )
    # Simulate an active fallback so success can be shown to reset it.
    agent._fallback_state.save = MagicMock()
    agent._fallback_state.consecutive_failures = 3
    agent._fallback_state.fallback_mode = True
    return agent, primary, new_provider, provider_factory, old_model


def test_switch_model_repoints_every_component(tmp_path):
    """A successful switch rebuilds the provider and repoints all consumers."""
    agent, primary, new_provider, _, _ = _make_switchable_agent(tmp_path)
    new_model = "anthropic/claude-opus-4-8"

    assert agent.switch_model(new_model, "api_key") is None

    assert agent.provider is new_provider
    assert agent.model == new_model
    assert agent.auth_method == "api_key"
    assert agent.compactor.provider is new_provider
    assert agent.compactor.model == new_model
    assert agent.context.model == new_model
    assert agent.subagents.provider is new_provider
    assert agent.subagents.model == new_model
    # A switch is a fresh start for failover bookkeeping.
    assert agent._fallback_state.consecutive_failures == 0
    assert agent._fallback_state.fallback_mode is False


def test_switch_model_keeps_old_provider_when_factory_raises(tmp_path):
    """A factory failure returns an error and leaves every consumer untouched."""
    agent, primary, _, _, old_model = _make_switchable_agent(tmp_path)

    def boom(model, auth):
        raise RuntimeError("no route to host")

    agent._provider_factory = boom

    result = agent.switch_model("anthropic/claude-opus-4-8", "api_key")
    assert result and "no route to host" in result

    assert agent.provider is primary
    assert agent.model == old_model
    assert agent.auth_method == "oauth"
    assert agent.compactor.provider is primary
    assert agent.compactor.model == old_model
    assert agent.context.model == old_model
    assert agent.subagents.provider is primary
    assert agent.subagents.model == old_model


# ── config_tool custom-model gate ────────────────────────────────

def _config_tool() -> ConfigTool:
    return ConfigTool(agent=MagicMock())


@pytest.mark.asyncio
async def test_config_tool_accepts_known_custom_model(monkeypatch):
    """Setting the primary model to a configured custom id passes the gate."""
    config = make_config()
    monkeypatch.setattr("ragnarbot.config.loader.load_config", lambda *a, **k: config)
    monkeypatch.setattr("ragnarbot.config.loader.save_config", lambda *a, **k: None)

    result = await _config_tool().execute(
        action="set",
        path="agents.defaults.model",
        value=custom_model_id("jetson", QWEN),
    )
    assert "is not available" not in result
    assert "does not match any configured custom server" not in result


@pytest.mark.asyncio
async def test_config_tool_rejects_unknown_custom_server(monkeypatch):
    """A custom id for a non-configured server is rejected with a clear error."""
    config = make_config()
    monkeypatch.setattr("ragnarbot.config.loader.load_config", lambda *a, **k: config)
    monkeypatch.setattr("ragnarbot.config.loader.save_config", lambda *a, **k: None)

    result = await _config_tool().execute(
        action="set",
        path="agents.defaults.model",
        value="custom/unknown/x",
    )
    assert "does not match any configured custom server" in result
