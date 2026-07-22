"""REST API for the web console modules: config, cron, hooks, agents, files, status."""

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import web

from ragnarbot import __version__

_WEB_CONFIG_PATHS = frozenset({"agents.defaults.experimental_soul"})
_WORKSPACE_TEXT_SUFFIXES = frozenset({
    ".cfg", ".conf", ".css", ".csv", ".html", ".ini", ".js", ".json",
    ".jsx", ".md", ".py", ".sh", ".skill", ".sql", ".toml", ".ts",
    ".tsx", ".txt", ".xml", ".yaml", ".yml",
})
_WORKSPACE_IMAGE_SUFFIXES = frozenset({
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp",
})
_WORKSPACE_VIDEO_SUFFIXES = frozenset({".m4v", ".mov", ".mp4", ".ogv", ".webm"})
_WORKSPACE_HIDDEN_DIRS = frozenset({"agents", "__pycache__", "node_modules"})
_WORKSPACE_SENSITIVE_STEMS = ("auth", "credential", "secret", "token", "private-key", "private_key")
_WORKSPACE_MAX_EDIT_BYTES = 2 * 1024 * 1024
_WORKSPACE_MAX_PREVIEW_BYTES = 100 * 1024 * 1024


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _int_query(request: web.Request, name: str, default: int, lo: int = 1, hi: int = 10000) -> int:
    """Parse an int query param, clamping to [lo, hi]; bad input falls back to default."""
    try:
        return max(lo, min(hi, int(request.query.get(name, default))))
    except (TypeError, ValueError):
        return default


class ApiRoutes:
    """All module REST endpoints. Thin wrappers over existing services."""

    def __init__(self, server: Any):
        self.server = server
        self.agent = server.agent
        self.config = server.config
        self.notifications = server.notifications

    def register(self, r: web.UrlDispatcher) -> None:
        # config & secrets
        r.add_get("/api/config/schema", self.config_schema)
        r.add_patch("/api/config", self.config_set)
        r.add_get("/api/secrets", self.secrets_list)
        r.add_put("/api/secrets", self.secrets_set)
        # models & providers
        r.add_get("/api/models", self.models_overview)
        r.add_get("/api/models/catalog/{provider_id}", self.models_catalog)
        r.add_post("/api/models/select", self.models_select)
        r.add_post("/api/models/custom", self.models_custom_add)
        r.add_patch("/api/models/custom/{server_id}", self.models_custom_update)
        r.add_delete("/api/models/custom/{server_id}", self.models_custom_delete)
        r.add_post("/api/models/custom/{server_id}/probe", self.models_custom_probe)
        # cron
        r.add_get("/api/cron", self.cron_list)
        r.add_patch("/api/cron/{job_id}", self.cron_update)
        r.add_delete("/api/cron/{job_id}", self.cron_delete)
        r.add_post("/api/cron/{job_id}/run", self.cron_run)
        r.add_get("/api/cron/{job_id}/logs", self.cron_logs)
        # hooks
        r.add_get("/api/hooks", self.hooks_list)
        r.add_post("/api/hooks", self.hooks_add)
        r.add_patch("/api/hooks/{hook_id}", self.hooks_update)
        r.add_delete("/api/hooks/{hook_id}", self.hooks_delete)
        r.add_get("/api/hooks/{hook_id}/history", self.hooks_history)
        # agents: run monitoring + read-only definitions
        r.add_get("/api/agents/tasks", self.agents_tasks)
        r.add_get("/api/agents/tasks/{task_id}", self.agents_task_get)
        r.add_post("/api/agents/tasks/{task_id}/stop", self.agents_task_stop)
        r.add_post("/api/agents/tasks/{task_id}/dismiss", self.agents_task_dismiss)
        r.add_post("/api/agents/tasks/{task_id}/message", self.agents_task_message)
        r.add_get("/api/agents/definitions", self.agents_definitions)
        # background jobs
        r.add_get("/api/jobs", self.jobs_list)
        r.add_get("/api/jobs/{job_id}/output", self.jobs_output)
        r.add_post("/api/jobs/{job_id}/kill", self.jobs_kill)
        r.add_post("/api/jobs/{job_id}/dismiss", self.jobs_dismiss)
        # workspace files & skills
        r.add_get("/api/workspace/tree", self.workspace_tree)
        r.add_get("/api/workspace/file", self.workspace_file_get)
        r.add_put("/api/workspace/file", self.workspace_file_put)
        r.add_get("/api/workspace/preview", self.workspace_file_preview)
        r.add_get("/api/workspace/download", self.workspace_file_download)
        r.add_get("/api/skills", self.skills_list)
        r.add_get("/api/skills/{name}", self.skills_get)
        r.add_put("/api/skills/{name}", self.skills_put)
        # status, logs, update, restart
        r.add_get("/api/status/full", self.status_full)
        r.add_get("/api/logs/tail", self.logs_tail)
        r.add_post("/api/update/check", self.update_check)
        r.add_post("/api/update/run", self.update_run)
        r.add_post("/api/restart", self.restart)
        # activity
        r.add_get("/api/notifications", self.notifications_list)
        r.add_post("/api/notifications/read", self.notifications_read)

    # ── config & secrets ─────────────────────────────────────────

    async def config_schema(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths, get_field_meta
        from ragnarbot.config.schema import Config

        config = load_config()
        all_paths = get_all_paths(config)
        fields = []
        for path in sorted(_WEB_CONFIG_PATHS):
            meta = get_field_meta(Config, path)
            default = meta.get("default")
            if default.__class__.__name__ == "PydanticUndefinedType":
                default = None
            fields.append({
                "path": path,
                "type": meta.get("type", "unknown"),
                "default": default,
                "value": all_paths[path],
                "reload": meta.get("reload"),
                "label": meta.get("label", ""),
            })
        return web.json_response(fields)

    async def config_set(self, request: web.Request) -> web.Response:
        body = await request.json()
        path, value = body.get("path"), body.get("value")
        if not path or value is None:
            return _json_error("path and value are required")
        if path not in _WEB_CONFIG_PATHS:
            return _json_error("this setting is managed through the agent", 403)
        result = await self._config_tool_set(path, value)
        return result

    async def _config_tool_set(self, path: str, value: Any) -> web.Response:
        config_tool = self.agent.tools.get("config")
        if config_tool is None:
            return _json_error("config tool unavailable", 500)
        result_str = await config_tool.execute(action="set", path=path, value=str(value))
        if result_str.startswith("Error"):
            return _json_error(result_str.removeprefix("Error: ").removeprefix("Error executing config: "))
        try:
            return web.json_response(json.loads(result_str))
        except json.JSONDecodeError:
            return web.json_response({"detail": result_str})

    async def secrets_list(self, request: web.Request) -> web.Response:
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.path_utils import get_all_paths
        from ragnarbot.config.providers import PROVIDERS

        creds = load_credentials()
        api_key_urls = {p["id"]: p.get("api_key_url", "") for p in PROVIDERS}
        entries = []
        for path, value in sorted(get_all_paths(creds).items()):
            if path.startswith("extra"):
                continue
            entry = {"path": path, "set": bool(value)}
            parts = path.split(".")
            if parts[0] == "providers" and len(parts) > 1:
                entry["api_key_url"] = api_key_urls.get(parts[1], "")
            entries.append(entry)
        extra = [{"path": f"extra.{k}", "set": True} for k in sorted(creds.extra)]
        return web.json_response({"secrets": entries, "extra": extra})

    async def secrets_set(self, request: web.Request) -> web.Response:
        body = await request.json()
        path, value = body.get("path"), body.get("value")
        if not path or value is None:
            return _json_error("path and value are required")
        if not path.startswith("secrets."):
            path = f"secrets.{path}"
        return await self._config_tool_set(path, value)

    # ── models & providers ───────────────────────────────────────

    async def models_overview(self, request: web.Request) -> web.Response:
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.providers import (
            PROVIDERS,
            custom_model_id,
            custom_provider_secret_name,
            supports_oauth,
        )

        config = load_config()
        creds = load_credentials()

        def oauth_connected(provider_id: str) -> bool:
            provider_creds = getattr(creds.providers, provider_id, None)
            if provider_id == "anthropic":
                return bool(provider_creds and provider_creds.oauth_key)
            if provider_id == "gemini":
                from ragnarbot.auth.gemini_oauth import is_authenticated
                return is_authenticated()
            if provider_id == "openai":
                from ragnarbot.auth.openai_oauth import is_authenticated
                return is_authenticated()
            return False

        providers = []
        for p in PROVIDERS:
            provider_creds = getattr(creds.providers, p["id"], None)
            key_set = bool(provider_creds and provider_creds.api_key)
            oauth_set = supports_oauth(p["id"]) and oauth_connected(p["id"])
            providers.append({
                "id": p["id"],
                "name": p["name"],
                "description": p["description"],
                "api_key_url": p.get("api_key_url", ""),
                "key_set": key_set,
                "oauth_set": oauth_set,
                "connected": key_set or oauth_set,
                "oauth_supported": supports_oauth(p["id"]),
                "models": [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "description": m.get("description", ""),
                        "oauth": m.get("oauth", True),
                    }
                    for m in p["models"]
                ],
            })

        custom = []
        for server in config.custom_providers:
            custom.append({
                "id": server.id,
                "name": server.name or server.id,
                "base_url": server.base_url,
                "api_key_set": bool(creds.extra.get(custom_provider_secret_name(server.id), "")),
                "models": [
                    {
                        "id": m.id,
                        "name": m.name or m.id,
                        "vision": m.vision,
                        "max_tokens": m.max_tokens,
                        "full_id": custom_model_id(server.id, m.id),
                    }
                    for m in server.models
                ],
            })

        defaults = config.agents.defaults
        return web.json_response({
            "current": {
                "model": defaults.model,
                "auth_method": defaults.auth_method,
                "reasoning_level": defaults.reasoning_level,
                "fallback_model": config.agents.fallback.model,
            },
            "providers": providers,
            "custom": custom,
        })

    # Filter obvious non-chat OpenAI models out of the catalog.
    _OPENAI_NON_CHAT = re.compile(
        r"embedding|whisper|tts|dall-e|audio|realtime|moderation|davinci|babbage|image|transcribe|search"
    )

    async def models_catalog(self, request: web.Request) -> web.Response:
        """Fetch the provider's live model catalog using stored credentials."""
        import aiohttp

        from ragnarbot.auth.credentials import load_credentials

        provider_id = request.match_info["provider_id"]
        creds = load_credentials()
        pc = getattr(creds.providers, provider_id, None)
        api_key = pc.api_key if pc else ""
        oauth_key = pc.oauth_key if pc else ""

        url: str
        headers: dict[str, str] = {}
        if provider_id == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif provider_id == "anthropic":
            url = "https://api.anthropic.com/v1/models?limit=100"
            headers["anthropic-version"] = "2023-06-01"
            if api_key:
                headers["x-api-key"] = api_key
            elif oauth_key:
                headers["Authorization"] = f"Bearer {oauth_key}"
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                return web.json_response({"ok": False, "error": "no Anthropic credentials configured"})
        elif provider_id == "openai":
            if not api_key:
                return web.json_response({
                    "ok": False,
                    "error": "listing OpenAI models needs an API key (ChatGPT OAuth cannot list models)",
                })
            url = "https://api.openai.com/v1/models"
            headers["Authorization"] = f"Bearer {api_key}"
        elif provider_id == "gemini":
            if not api_key:
                return web.json_response({"ok": False, "error": "listing Gemini models needs an API key"})
            url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={api_key}"
        else:
            raise web.HTTPNotFound()

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"ok": False, "error": f"provider returned HTTP {resp.status}"}
                        )
                    payload = await resp.json(content_type=None)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

        models: list[dict] = []
        if provider_id == "openrouter":
            for m in payload.get("data", []):
                mid = m.get("id")
                if not mid:
                    continue
                pricing = m.get("pricing") or {}
                free = mid.endswith(":free") or (
                    str(pricing.get("prompt", "1")) in ("0", "0.0")
                    and str(pricing.get("completion", "1")) in ("0", "0.0")
                )
                models.append({
                    "id": f"openrouter/{mid}",
                    "name": m.get("name") or mid,
                    "description": (m.get("description") or "").split(". ")[0][:120],
                    "free": free,
                })
        elif provider_id == "anthropic":
            for m in payload.get("data", []):
                mid = m.get("id")
                if not mid:
                    continue
                models.append({
                    "id": f"anthropic/{mid}",
                    "name": m.get("display_name") or mid,
                    "description": "",
                    "free": False,
                })
        elif provider_id == "openai":
            for m in payload.get("data", []):
                mid = m.get("id") or ""
                if not mid or self._OPENAI_NON_CHAT.search(mid):
                    continue
                models.append({"id": f"openai/{mid}", "name": mid, "description": "", "free": False})
            models.sort(key=lambda m: m["id"])
        elif provider_id == "gemini":
            for m in payload.get("models", []):
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                mid = (m.get("name") or "").removeprefix("models/")
                if not mid:
                    continue
                models.append({
                    "id": f"gemini/{mid}",
                    "name": m.get("displayName") or mid,
                    "description": (m.get("description") or "")[:120],
                    "free": False,
                })

        return web.json_response({"ok": True, "models": models})

    async def _broadcast_state(self) -> None:
        """Push a fresh state event (model, reasoning, context) to open web chats."""
        channel = getattr(self.server, "channel", None)
        build_state = getattr(self.server, "_build_state", None)
        if channel is None or not callable(build_state):
            return
        try:
            await channel.broadcast({"type": "state", **build_state()})
        except Exception:
            from loguru import logger
            logger.exception("Failed to broadcast state after model switch")

    @staticmethod
    def _pick_auth_method(model: str, preferred: str | None) -> tuple[str | None, str | None]:
        """Find an auth method whose credentials work for `model`.

        Tries the preferred/current method first, then the alternative.
        Returns (auth_method, None) on success or (None, error) when neither works.
        """
        from ragnarbot.config.validation import validate_model_auth

        candidates = []
        for method in (preferred, "oauth", "api_key"):
            if method in ("oauth", "api_key") and method not in candidates:
                candidates.append(method)
        last_error = None
        for method in candidates:
            error = validate_model_auth(model, method)
            if error is None:
                return method, None
            last_error = error
        return None, last_error

    async def models_select(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config, save_config

        body = await request.json()
        model = body.get("model")
        target = body.get("target", "primary")
        if target not in ("primary", "fallback"):
            return _json_error("target must be 'primary' or 'fallback'")
        if model is None or (target == "primary" and not model):
            return _json_error("model is required")

        config = load_config()

        if target == "fallback":
            fallback = config.agents.fallback
            if model:
                auth, error = self._pick_auth_method(model, body.get("auth_method") or fallback.auth_method)
                if auth is None:
                    return _json_error(error or "no working credentials for this model")
                fallback.model, fallback.auth_method = model, auth
            else:
                fallback.model = None  # empty string clears the fallback
            save_config(config)
            # Mirror the config tool's warm reload on the live agent.
            agent = self.agent
            if hasattr(agent, "_fallback_model"):
                agent._fallback_model = fallback.model
            if hasattr(agent, "_fallback_provider"):
                agent._fallback_provider = None
            live = getattr(agent, "_fallback_config", None)
            if live is not None:
                live.model, live.auth_method = fallback.model, fallback.auth_method
            detail = "Fallback model updated." if model else "Fallback model cleared."
            return web.json_response({"status": "applied", "detail": detail})

        defaults = config.agents.defaults
        auth, error = self._pick_auth_method(model, body.get("auth_method") or defaults.auth_method)
        if auth is None:
            return _json_error(error or "no working credentials for this model")
        defaults.model, defaults.auth_method = model, auth
        save_config(config)

        # Hot-apply on the live agent; fall back to restart-required if that fails.
        switch = getattr(self.agent, "switch_model", None)
        switch_error = switch(model, auth) if callable(switch) else "agent unavailable"
        if switch_error is None:
            await self._broadcast_state()
            return web.json_response({
                "status": "applied",
                "detail": f"Switched to {model} — active now.",
                "auth_method": auth,
                "restart_required": False,
            })
        return web.json_response({
            "status": "saved",
            "detail": f"Saved, but hot reload failed ({switch_error}). Restart the gateway to apply.",
            "auth_method": auth,
            "restart_required": True,
        })

    @staticmethod
    def _parse_custom_models(raw: Any) -> list[dict] | web.Response:
        """Normalize a request's models payload into CustomModelConfig dicts."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            return _json_error("models must be a list")
        models = []
        for item in raw:
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, dict) or not str(item.get("id", "")).strip():
                return _json_error("each model needs a non-empty id")
            max_tokens = item.get("max_tokens")
            models.append({
                "id": str(item["id"]).strip(),
                "name": str(item.get("name", "")).strip(),
                "vision": bool(item.get("vision", False)),
                "max_tokens": int(max_tokens) if max_tokens else None,
            })
        return models

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        url = (raw or "").strip().rstrip("/")
        return url

    def _save_custom_api_key(self, server_id: str, api_key: Any) -> None:
        """Persist (or clear) a custom server's API key in credentials.extra."""
        from ragnarbot.auth.credentials import load_credentials, save_credentials
        from ragnarbot.config.providers import custom_provider_secret_name

        creds = load_credentials()
        name = custom_provider_secret_name(server_id)
        if api_key:
            creds.extra[name] = str(api_key)
        else:
            creds.extra.pop(name, None)
        save_credentials(creds)

    async def models_custom_add(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config, save_config
        from ragnarbot.config.schema import CustomProviderConfig

        body = await request.json()
        name = str(body.get("name", "")).strip()
        # Lowercase alnum + underscore only: the id is embedded in model ids
        # (custom/<id>/<model>) and credential key names.
        raw_id = str(body.get("id") or "").strip() or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", raw_id):
            return _json_error("a valid server id is required (lowercase letters, digits, _)")
        server_id = raw_id
        base_url = self._normalize_base_url(body.get("base_url", ""))
        if not base_url.startswith(("http://", "https://")):
            return _json_error("base_url must start with http:// or https://")
        models = self._parse_custom_models(body.get("models"))
        if isinstance(models, web.Response):
            return models

        config = load_config()
        if any(s.id == server_id for s in config.custom_providers):
            return _json_error(f"custom server '{server_id}' already exists", 409)
        config.custom_providers.append(CustomProviderConfig(
            id=server_id, name=name or server_id, base_url=base_url, models=models,
        ))
        save_config(config)
        if "api_key" in body:
            self._save_custom_api_key(server_id, body.get("api_key"))
        return web.json_response({"ok": True, "id": server_id})

    async def models_custom_update(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config, save_config
        from ragnarbot.config.schema import CustomModelConfig

        server_id = request.match_info["server_id"]
        body = await request.json()
        config = load_config()
        server = next((s for s in config.custom_providers if s.id == server_id), None)
        if server is None:
            raise web.HTTPNotFound()
        if "name" in body:
            server.name = str(body["name"]).strip()
        if "base_url" in body:
            base_url = self._normalize_base_url(body["base_url"])
            if not base_url.startswith(("http://", "https://")):
                return _json_error("base_url must start with http:// or https://")
            server.base_url = base_url
        if "models" in body:
            models = self._parse_custom_models(body["models"])
            if isinstance(models, web.Response):
                return models
            server.models = [CustomModelConfig(**m) for m in models]
        save_config(config)
        if "api_key" in body:
            self._save_custom_api_key(server_id, body.get("api_key"))
        return web.json_response({"ok": True})

    async def models_custom_delete(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config, save_config

        server_id = request.match_info["server_id"]
        config = load_config()
        server = next((s for s in config.custom_providers if s.id == server_id), None)
        if server is None:
            raise web.HTTPNotFound()
        prefix = f"custom/{server_id}/"
        in_use = [
            label for label, value in (
                ("primary model", config.agents.defaults.model),
                ("fallback model", config.agents.fallback.model),
            )
            if value and value.startswith(prefix)
        ]
        if in_use:
            return _json_error(
                f"server '{server_id}' is in use as the {' and '.join(in_use)} — switch models first",
                409,
            )
        config.custom_providers = [s for s in config.custom_providers if s.id != server_id]
        save_config(config)
        self._save_custom_api_key(server_id, None)
        return web.json_response({"ok": True})

    async def models_custom_probe(self, request: web.Request) -> web.Response:
        """Query a custom server's /models endpoint; optionally merge new ids."""
        import aiohttp

        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.loader import load_config, save_config
        from ragnarbot.config.providers import custom_provider_secret_name
        from ragnarbot.config.schema import CustomModelConfig

        server_id = request.match_info["server_id"]
        config = load_config()
        server = next((s for s in config.custom_providers if s.id == server_id), None)
        if server is None:
            raise web.HTTPNotFound()

        headers = {}
        api_key = load_credentials().extra.get(custom_provider_secret_name(server_id), "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(f"{server.base_url}/models", headers=headers) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"ok": False, "error": f"server returned HTTP {resp.status}"}
                        )
                    payload = await resp.json(content_type=None)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

        raw_models = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw_models, list):
            return web.json_response({"ok": False, "error": "unexpected /models response shape"})
        discovered = [
            str(m["id"]) for m in raw_models
            if isinstance(m, dict) and m.get("id")
        ]

        raw_body = await request.text()
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {}

        added: list[str] = []
        if isinstance(body, dict) and body.get("save"):
            # The probe held the config across a network await — reload so the
            # save doesn't clobber concurrent config writes.
            config = load_config()
            server = next((s for s in config.custom_providers if s.id == server_id), None)
            if server is None:
                raise web.HTTPNotFound()
            known = {m.id for m in server.models}
            for mid in discovered:
                if mid not in known:
                    server.models.append(CustomModelConfig(id=mid))
                    added.append(mid)
            if added:
                save_config(config)

        return web.json_response({"ok": True, "models": discovered, "added": added})

    # ── cron ─────────────────────────────────────────────────────

    @property
    def cron(self):
        return self.agent.cron_service

    async def cron_list(self, request: web.Request) -> web.Response:
        if self.cron is None:
            return web.json_response([])
        return web.json_response([asdict(j) for j in self.cron.list_jobs(include_disabled=True)])

    async def cron_update(self, request: web.Request) -> web.Response:
        if self.cron is None:
            return _json_error("cron service unavailable", 500)
        job_id = request.match_info["job_id"]
        body = await request.json()
        if set(body) != {"enabled"}:
            return _json_error("the web console only supports enabling or pausing cron jobs")
        job = self.cron.enable_job(job_id, bool(body["enabled"]))
        if job is None:
            raise web.HTTPNotFound()
        return web.json_response(asdict(job))

    async def cron_delete(self, request: web.Request) -> web.Response:
        if self.cron is None or not self.cron.remove_job(request.match_info["job_id"]):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def cron_run(self, request: web.Request) -> web.Response:
        if self.cron is None:
            return _json_error("cron service unavailable", 500)
        ok = await self.cron.run_job(request.match_info["job_id"], force=True)
        return web.json_response({"ok": ok})

    async def cron_logs(self, request: web.Request) -> web.Response:
        from ragnarbot.cron.logger import get_cron_logs_dir

        job_id = request.match_info["job_id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
            return _json_error("invalid job id")
        entries: list[dict] = []
        path = get_cron_logs_dir() / f"{job_id}.jsonl"
        if path.is_file():
            with open(path) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        entries.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return web.json_response(entries[:50])

    # ── hooks ────────────────────────────────────────────────────

    @property
    def hooks(self):
        return self.agent.hook_service

    async def hooks_list(self, request: web.Request) -> web.Response:
        if self.hooks is None:
            return web.json_response([])
        return web.json_response(
            [asdict(h) for h in self.hooks.list_hooks(include_disabled=True)]
        )

    async def hooks_add(self, request: web.Request) -> web.Response:
        if self.hooks is None:
            return _json_error("hooks service unavailable", 500)
        body = await request.json()
        if not body.get("name"):
            return _json_error("name is required")
        hook = self.hooks.add_hook(
            name=body["name"],
            instructions=body.get("instructions", ""),
            mode=body.get("mode", "alert"),
            channel=body.get("channel"),
            to=body.get("to"),
        )
        return web.json_response(asdict(hook))

    async def hooks_update(self, request: web.Request) -> web.Response:
        if self.hooks is None:
            return _json_error("hooks service unavailable", 500)
        body = await request.json()
        allowed = {k: v for k, v in body.items()
                   if k in ("name", "instructions", "mode", "enabled", "channel", "to")}
        hook = self.hooks.update_hook(request.match_info["hook_id"], **allowed)
        if hook is None:
            raise web.HTTPNotFound()
        return web.json_response(asdict(hook))

    async def hooks_delete(self, request: web.Request) -> web.Response:
        if self.hooks is None or not self.hooks.delete_hook(request.match_info["hook_id"]):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def hooks_history(self, request: web.Request) -> web.Response:
        if self.hooks is None:
            return web.json_response([])
        return web.json_response(
            self.hooks.get_history(request.match_info["hook_id"], limit=50)
        )

    # ── agents: runs + read-only definitions ─────────────────────

    async def agents_tasks(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.subagents.list_tasks())

    async def agents_task_get(self, request: web.Request) -> web.Response:
        progress = self.agent.subagents.get_progress(request.match_info["task_id"])
        return web.json_response(progress, dumps=lambda o: json.dumps(o, default=str))

    async def agents_task_stop(self, request: web.Request) -> web.Response:
        result = await self.agent.subagents.stop_task(request.match_info["task_id"])
        return web.json_response({"detail": result})

    async def agents_task_dismiss(self, request: web.Request) -> web.Response:
        result = self.agent.subagents.dismiss_task(request.match_info["task_id"])
        return web.json_response({"detail": result})

    async def agents_task_message(self, request: web.Request) -> web.Response:
        body = await request.json()
        content = body.get("content", "")
        if not content:
            return _json_error("content is required")
        result = await self.agent.subagents.send_message(
            request.match_info["task_id"], content,
        )
        return web.json_response({"detail": result})

    async def agents_definitions(self, request: web.Request) -> web.Response:
        loader = self.agent.context.agents
        definitions = []
        for item in loader.list_agents():
            definition = loader.load_agent(item["name"])
            if definition is None:
                continue
            definitions.append({
                "name": definition.name,
                "description": definition.description,
                "source": item["source"],
                "path": definition.path,
                "config": {
                    "model": definition.model,
                    "reasoning_level": definition.reasoning_level,
                    "allowed_tools": definition.allowed_tools,
                    "allowed_skills": definition.allowed_skills,
                },
                "instructions": definition.body,
            })
        return web.json_response(definitions)

    async def jobs_list(self, request: web.Request) -> web.Response:
        return web.json_response({"summary": self.agent.bg_processes.get_status_summary()})

    async def jobs_output(self, request: web.Request) -> web.Response:
        lines = _int_query(request, "lines", 50, hi=1000)
        output = self.agent.bg_processes.get_output(request.match_info["job_id"], lines=lines)
        return web.json_response({"output": output})

    async def jobs_kill(self, request: web.Request) -> web.Response:
        result = await self.agent.bg_processes.kill(request.match_info["job_id"])
        return web.json_response({"detail": result})

    async def jobs_dismiss(self, request: web.Request) -> web.Response:
        result = self.agent.bg_processes.dismiss(request.match_info["job_id"])
        return web.json_response({"detail": result})

    # ── workspace files & skills ─────────────────────────────────

    @property
    def workspace(self) -> Path:
        return Path(self.agent.workspace)

    @staticmethod
    def _workspace_file_kind(rel: Path) -> str | None:
        """Return the safe viewer kind for a workspace-relative path."""
        if not rel.parts or any(part.startswith(".") for part in rel.parts):
            return None
        if any(part in _WORKSPACE_HIDDEN_DIRS for part in rel.parts[:-1]):
            return None
        stem = rel.stem.lower()
        if "sanitized" not in stem and any(
            stem == prefix or stem.startswith(f"{prefix}-") or stem.startswith(f"{prefix}_")
            for prefix in _WORKSPACE_SENSITIVE_STEMS
        ):
            return None
        suffix = rel.suffix.lower()
        if suffix in _WORKSPACE_TEXT_SUFFIXES:
            return "text"
        if suffix in _WORKSPACE_IMAGE_SUFFIXES:
            return "image"
        if suffix in _WORKSPACE_VIDEO_SUFFIXES:
            return "video"
        return None

    @classmethod
    def _workspace_file_is_exposed(cls, rel: Path) -> bool:
        """Keep Files focused on safe text documents, source files, and browser media."""
        return cls._workspace_file_kind(rel) is not None

    def _workspace_path(
        self,
        raw: str,
        *,
        exposed_file: bool = False,
        editable_file: bool = False,
    ) -> Path:
        path = (self.workspace / raw).resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise web.HTTPForbidden(reason="path outside workspace")
        rel = path.relative_to(self.workspace.resolve())
        if rel.parts and rel.parts[0] == "agents":
            raise web.HTTPForbidden(reason="agent definitions are not exposed in the web console")
        if exposed_file and not self._workspace_file_is_exposed(rel):
            raise web.HTTPForbidden(reason="file is not exposed in the web console")
        if editable_file and self._workspace_file_kind(rel) != "text":
            raise web.HTTPUnsupportedMediaType(reason="file is not editable text")
        return path

    async def workspace_tree(self, request: web.Request) -> web.Response:
        root = self.workspace.resolve()
        files: list[tuple[Path, str, os.stat_result, bool]] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                dirname
                for dirname in dirnames
                if not dirname.startswith(".")
                and dirname not in _WORKSPACE_HIDDEN_DIRS
                and not (current_path / dirname).is_symlink()
            )
            for filename in sorted(filenames):
                path = current_path / filename
                if path.is_symlink():
                    continue
                rel = path.relative_to(root)
                kind = self._workspace_file_kind(rel)
                if kind is None:
                    continue
                stat = path.stat()
                if kind == "text" and stat.st_size > _WORKSPACE_MAX_EDIT_BYTES:
                    continue
                previewable = kind == "text" or stat.st_size <= _WORKSPACE_MAX_PREVIEW_BYTES
                files.append((path, kind, stat, previewable))

        if not files:
            return web.json_response([])

        directories = {
            parent
            for path, _kind, _stat, _previewable in files
            for parent in path.relative_to(root).parents
            if parent != Path(".")
        }
        entries = [
            {
                "path": str(rel),
                "dir": True,
                "kind": "directory",
                "size": None,
                "modified": None,
                "previewable": False,
            }
            for rel in sorted(directories, key=str)
        ]
        entries.extend(
            {
                "path": str(path.relative_to(root)),
                "dir": False,
                "kind": kind,
                "size": stat.st_size,
                "modified": round(stat.st_mtime * 1000),
                "previewable": previewable,
            }
            for path, kind, stat, previewable in files
        )
        entries.sort(key=lambda entry: entry["path"])
        return web.json_response(entries)

    async def workspace_file_get(self, request: web.Request) -> web.Response:
        path = self._workspace_path(
            request.query.get("path", ""), exposed_file=True, editable_file=True
        )
        if not path.is_file():
            raise web.HTTPNotFound()
        if path.stat().st_size > _WORKSPACE_MAX_EDIT_BYTES:
            return _json_error("file too large to edit", 413)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _json_error("not a text file", 415)
        return web.json_response({"path": request.query["path"], "content": content})

    async def workspace_file_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        raw = body.get("path", "")
        if not raw:
            return _json_error("path is required")
        path = self._workspace_path(raw, exposed_file=True, editable_file=True)
        if not path.is_file():
            raise web.HTTPNotFound()
        content = body.get("content", "")
        if not isinstance(content, str):
            return _json_error("content must be a string")
        if len(content.encode("utf-8")) > _WORKSPACE_MAX_EDIT_BYTES:
            return _json_error("file too large to edit", 413)
        path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})

    async def workspace_file_preview(self, request: web.Request) -> web.StreamResponse:
        raw = request.query.get("path", "")
        path = self._workspace_path(raw, exposed_file=True)
        if not path.is_file():
            raise web.HTTPNotFound()
        rel = path.relative_to(self.workspace.resolve())
        if self._workspace_file_kind(rel) not in {"image", "video"}:
            raise web.HTTPUnsupportedMediaType(reason="preview is only available for media files")
        if path.stat().st_size > _WORKSPACE_MAX_PREVIEW_BYTES:
            return _json_error("media file is larger than the 100 MB preview limit", 413)
        filename = path.name.replace('"', "") or "preview"
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def workspace_file_download(self, request: web.Request) -> web.StreamResponse:
        path = self._workspace_path(request.query.get("path", ""), exposed_file=True)
        if not path.is_file():
            raise web.HTTPNotFound()
        filename = path.name.replace('"', "") or "download"
        return web.FileResponse(
            path,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def skills_list(self, request: web.Request) -> web.Response:
        skills = self.agent.context.skills.list_skills(filter_unavailable=False)
        return web.json_response([skill for skill in skills if skill.get("source") == "workspace"])

    async def skills_get(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        path = self.workspace / "skills" / name / "SKILL.md" if name else None
        if path is None or not path.is_file():
            raise web.HTTPNotFound()
        return web.json_response({"name": name, "content": path.read_text(encoding="utf-8")})

    async def skills_put(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        if not name:
            return _json_error("invalid skill name")
        body = await request.json()
        content = body.get("content", "")
        if not content.strip():
            return _json_error("content is required")
        path = self.workspace / "skills" / name / "SKILL.md"
        builtin_root = getattr(self.agent.context.skills, "builtin_skills", None)
        builtin_path = Path(builtin_root) / name / "SKILL.md" if builtin_root else None
        if not path.exists() and builtin_path is not None and builtin_path.exists():
            return _json_error("builtin skills are not editable in the web console", 403)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True, "path": str(path)})

    # ── status, logs, update, restart ────────────────────────────

    async def status_full(self, request: web.Request) -> web.Response:
        from ragnarbot.instance import get_instance

        instance = get_instance()

        return web.json_response({
            "version": __version__,
            "profile": instance.profile,
            "workspace": str(self.agent.workspace),
            "hooks": {
                "enabled": self.config.hooks.enabled,
                "port": self.config.hooks.port,
                "count": len(self.hooks.list_hooks(include_disabled=True)) if self.hooks else 0,
            },
        })

    async def logs_tail(self, request: web.Request) -> web.Response:
        from ragnarbot.instance import get_instance

        lines = _int_query(request, "lines", 200, hi=2000)
        log_path = get_instance().log_dir / "gateway.log"
        if not log_path.is_file():
            return web.json_response({"lines": [], "path": str(log_path)})
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
        except Exception as e:
            return _json_error(str(e), 500)
        return web.json_response({"lines": tail[-lines:], "path": str(log_path)})

    async def update_check(self, request: web.Request) -> web.Response:
        update_tool = self.agent.tools.get("update")
        if update_tool is None:
            return _json_error("update tool unavailable", 500)
        result = await update_tool.execute(action="check")
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            return _json_error("update check returned an invalid response", 500)
        if not isinstance(payload, dict):
            return _json_error("update check returned an invalid response", 500)
        return web.json_response(payload)

    async def update_run(self, request: web.Request) -> web.Response:
        update_tool = self.agent.tools.get("update")
        if update_tool is None:
            return _json_error("update tool unavailable", 500)
        update_tool.set_context("web", "main")
        result = await update_tool.execute(action="update")
        return web.json_response({"detail": result})

    async def restart(self, request: web.Request) -> web.Response:
        restart_tool = self.agent.tools.get("restart")
        if restart_tool is None:
            return _json_error("restart tool unavailable", 500)
        restart_tool.set_context("web", "main")
        result = await restart_tool.execute()
        return web.json_response({"detail": result})

    # ── notifications & usage ────────────────────────────────────

    async def notifications_list(self, request: web.Request) -> web.Response:
        if self.notifications is None:
            return web.json_response({"items": [], "unread": 0})
        items = self.notifications.list(
            limit=_int_query(request, "limit", 50, hi=500),
            before=request.query.get("before"),
            kind=request.query.get("kind"),
        )
        return web.json_response({
            "items": items,
            "unread": self.notifications.unread_count(),
        })

    async def notifications_read(self, request: web.Request) -> web.Response:
        if self.notifications is None:
            return _json_error("notifications unavailable", 500)
        body = await request.json()
        if body.get("all"):
            self.notifications.mark_read(None)
        elif isinstance(body.get("ids"), list):
            self.notifications.mark_read(body["ids"])
        else:
            return _json_error("either all:true or ids:[...] is required")
        return web.json_response({"ok": True, "unread": self.notifications.unread_count()})

def _safe_name(name: str) -> str | None:
    """Validate a skill/agent directory name (no path tricks)."""
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name or ""):
        return name
    return None
