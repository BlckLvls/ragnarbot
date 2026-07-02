"""REST API for the web console modules: config, cron, hooks, agents, files, status."""

import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from ragnarbot import __version__

_ENUM_PATTERN = re.compile(r"^\^\(([^)]+)\)\$$")


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


class ApiRoutes:
    """All module REST endpoints. Thin wrappers over existing services."""

    def __init__(self, server: Any):
        self.server = server
        self.agent = server.agent
        self.config = server.config
        self.notifications = server.notifications
        self.heartbeat = server.heartbeat

    def register(self, r: web.UrlDispatcher) -> None:
        # config & secrets
        r.add_get("/api/config/schema", self.config_schema)
        r.add_get("/api/config", self.config_list)
        r.add_get("/api/config/diff", self.config_diff)
        r.add_patch("/api/config", self.config_set)
        r.add_get("/api/secrets", self.secrets_list)
        r.add_put("/api/secrets", self.secrets_set)
        r.add_post("/api/secrets/reveal", self.secrets_reveal)
        # cron
        r.add_get("/api/cron", self.cron_list)
        r.add_post("/api/cron", self.cron_add)
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
        # agents (definitions + live runs)
        r.add_get("/api/agents/defs", self.agents_defs)
        r.add_get("/api/agents/defs/{name}", self.agents_def_get)
        r.add_put("/api/agents/defs/{name}", self.agents_def_put)
        r.add_get("/api/agents/tasks", self.agents_tasks)
        r.add_get("/api/agents/tasks/{task_id}", self.agents_task_get)
        r.add_post("/api/agents/tasks/{task_id}/stop", self.agents_task_stop)
        r.add_post("/api/agents/tasks/{task_id}/dismiss", self.agents_task_dismiss)
        r.add_post("/api/agents/tasks/{task_id}/message", self.agents_task_message)
        # background jobs
        r.add_get("/api/jobs", self.jobs_list)
        r.add_get("/api/jobs/{job_id}/output", self.jobs_output)
        r.add_post("/api/jobs/{job_id}/kill", self.jobs_kill)
        r.add_post("/api/jobs/{job_id}/dismiss", self.jobs_dismiss)
        # workspace files & memory & skills & recall
        r.add_get("/api/workspace/tree", self.workspace_tree)
        r.add_get("/api/workspace/file", self.workspace_file_get)
        r.add_put("/api/workspace/file", self.workspace_file_put)
        r.add_get("/api/skills", self.skills_list)
        r.add_get("/api/skills/{name}", self.skills_get)
        r.add_put("/api/skills/{name}", self.skills_put)
        r.add_post("/api/recall/search", self.recall_search)
        r.add_post("/api/heartbeat/trigger", self.heartbeat_trigger)
        # status, logs, update, restart
        r.add_get("/api/status/full", self.status_full)
        r.add_get("/api/logs/tail", self.logs_tail)
        r.add_post("/api/update/check", self.update_check)
        r.add_post("/api/update/run", self.update_run)
        r.add_post("/api/restart", self.restart)
        # notifications & usage
        r.add_get("/api/notifications", self.notifications_list)
        r.add_post("/api/notifications/read", self.notifications_read)
        r.add_get("/api/usage", self.usage)

    # ── config & secrets ─────────────────────────────────────────

    async def config_schema(self, request: web.Request) -> web.Response:
        from ragnarbot.agent.tools.secrets_helpers import CONFIG_DEPENDENCIES
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths, get_field_meta
        from ragnarbot.config.providers import PROVIDERS, supports_oauth
        from ragnarbot.config.schema import Config

        config = load_config()
        all_paths = get_all_paths(config)
        model_fields = {"agents.defaults.model", "agents.fallback.model"}

        model_options = [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m.get("description", ""),
                "provider": p["id"],
                "provider_name": p["name"],
                "vision": m.get("vision", True),
                "oauth": supports_oauth(p["id"]),
            }
            for p in PROVIDERS for m in p["models"]
        ]

        deps_by_path: dict[str, list[dict]] = {}
        for dep in CONFIG_DEPENDENCIES:
            deps_by_path.setdefault(dep.config_path, []).append({
                "value": dep.value_match,
                "match": dep.match_mode,
                "creds_paths": list(dep.creds_paths),
                "hint": dep.error_msg,
            })

        fields = []
        for p in sorted(all_paths.keys()):
            try:
                meta = get_field_meta(Config, p)
            except ValueError:
                continue
            default = meta.get("default")
            if default.__class__.__name__ == "PydanticUndefinedType":
                default = None
            field: dict[str, Any] = {
                "path": p,
                "type": meta.get("type", "unknown"),
                "default": default,
                "value": all_paths[p],
                "reload": meta.get("reload"),
                "label": meta.get("label", ""),
            }
            pattern = meta.get("pattern")
            if pattern:
                field["pattern"] = pattern
                m = _ENUM_PATTERN.match(pattern)
                if m:
                    field["enum"] = m.group(1).split("|")
            for key in ("ge", "le"):
                if meta.get(key) is not None:
                    field[key] = meta[key]
            if p in model_fields:
                field["options"] = model_options
            if p in deps_by_path:
                field["depends_on"] = deps_by_path[p]
            fields.append(field)
        return web.json_response(fields)

    async def config_list(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths
        return web.json_response(get_all_paths(load_config()))

    async def config_diff(self, request: web.Request) -> web.Response:
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths
        from ragnarbot.config.schema import Config

        current = get_all_paths(load_config())
        defaults = get_all_paths(Config())
        diffs = [
            {"path": p, "default": defaults.get(p), "current": v}
            for p, v in sorted(current.items())
            if v != defaults.get(p)
        ]
        return web.json_response(diffs)

    async def config_set(self, request: web.Request) -> web.Response:
        body = await request.json()
        path, value = body.get("path"), body.get("value")
        if not path or value is None:
            return _json_error("path and value are required")
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

    async def secrets_reveal(self, request: web.Request) -> web.Response:
        from ragnarbot.agent.tools.secrets_helpers import secrets_get
        from ragnarbot.auth.credentials import load_credentials

        body = await request.json()
        path = (body.get("path") or "").removeprefix("secrets.")
        if not path:
            return _json_error("path is required")
        result = secrets_get(load_credentials(), path)
        if result.startswith("Error"):
            return _json_error(result, 404)
        return web.json_response(json.loads(result))

    # ── cron ─────────────────────────────────────────────────────

    @property
    def cron(self):
        return self.agent.cron_service

    async def cron_list(self, request: web.Request) -> web.Response:
        if self.cron is None:
            return web.json_response([])
        return web.json_response([asdict(j) for j in self.cron.list_jobs(include_disabled=True)])

    async def cron_add(self, request: web.Request) -> web.Response:
        from ragnarbot.cron.types import CronSchedule

        if self.cron is None:
            return _json_error("cron service unavailable", 500)
        body = await request.json()
        try:
            schedule = CronSchedule(**body["schedule"])
        except (KeyError, TypeError) as e:
            return _json_error(f"invalid schedule: {e}")
        error = _validate_schedule(schedule)
        if error:
            return _json_error(error)
        job = self.cron.add_job(
            name=body.get("name", "unnamed"),
            schedule=schedule,
            message=body.get("message", ""),
            mode=body.get("mode", "isolated"),
            deliver=bool(body.get("deliver", False)),
            channel=body.get("channel"),
            to=body.get("to"),
            delete_after_run=bool(body.get("delete_after_run", False)),
            agent=body.get("agent"),
        )
        return web.json_response(asdict(job))

    async def cron_update(self, request: web.Request) -> web.Response:
        if self.cron is None:
            return _json_error("cron service unavailable", 500)
        job_id = request.match_info["job_id"]
        body = await request.json()
        if "enabled" in body and len(body) == 1:
            job = self.cron.enable_job(job_id, bool(body["enabled"]))
        else:
            allowed = {k: v for k, v in body.items()
                       if k in ("name", "message", "mode", "agent", "enabled", "schedule")}
            job = self.cron.update_job(job_id, **allowed)
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

    # ── agents: definitions ──────────────────────────────────────

    async def agents_defs(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.context.agents.list_agents())

    async def agents_def_get(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        definition = self.agent.context.agents.load_agent(name)
        if definition is None:
            raise web.HTTPNotFound()
        data = asdict(definition)
        try:
            data["content"] = Path(definition.path).read_text(encoding="utf-8")
        except OSError:
            data["content"] = ""
        return web.json_response(data)

    async def agents_def_put(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        if not name:
            return _json_error("invalid agent name")
        body = await request.json()
        content = body.get("content", "")
        if not content.strip():
            return _json_error("content is required")
        path = Path(self.agent.workspace) / "agents" / name / "AGENT.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True, "path": str(path)})

    # ── agents: live runs + background jobs ──────────────────────

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

    async def jobs_list(self, request: web.Request) -> web.Response:
        return web.json_response({"summary": self.agent.bg_processes.get_status_summary()})

    async def jobs_output(self, request: web.Request) -> web.Response:
        lines = int(request.query.get("lines", "50"))
        output = self.agent.bg_processes.get_output(request.match_info["job_id"], lines=lines)
        return web.json_response({"output": output})

    async def jobs_kill(self, request: web.Request) -> web.Response:
        result = await self.agent.bg_processes.kill(request.match_info["job_id"])
        return web.json_response({"detail": result})

    async def jobs_dismiss(self, request: web.Request) -> web.Response:
        result = self.agent.bg_processes.dismiss(request.match_info["job_id"])
        return web.json_response({"detail": result})

    # ── workspace, memory, skills, recall ────────────────────────

    @property
    def workspace(self) -> Path:
        return Path(self.agent.workspace)

    def _workspace_path(self, raw: str) -> Path:
        path = (self.workspace / raw).resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise web.HTTPForbidden(reason="path outside workspace")
        return path

    async def workspace_tree(self, request: web.Request) -> web.Response:
        root = self.workspace
        entries = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if len(rel.parts) > 6:
                continue
            entries.append({
                "path": str(rel),
                "dir": path.is_dir(),
                "size": path.stat().st_size if path.is_file() else None,
            })
        return web.json_response(entries)

    async def workspace_file_get(self, request: web.Request) -> web.Response:
        path = self._workspace_path(request.query.get("path", ""))
        if not path.is_file():
            raise web.HTTPNotFound()
        if path.stat().st_size > 2 * 1024 * 1024:
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
        path = self._workspace_path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    async def skills_list(self, request: web.Request) -> web.Response:
        return web.json_response(
            self.agent.context.skills.list_skills(filter_unavailable=False)
        )

    async def skills_get(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        content = self.agent.context.skills.load_skill(name) if name else None
        if content is None:
            raise web.HTTPNotFound()
        return web.json_response({"name": name, "content": content})

    async def skills_put(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        if not name:
            return _json_error("invalid skill name")
        body = await request.json()
        content = body.get("content", "")
        if not content.strip():
            return _json_error("content is required")
        path = self.workspace / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True, "path": str(path)})

    async def recall_search(self, request: web.Request) -> web.Response:
        body = await request.json()
        query = (body.get("query") or "").strip()
        if not query:
            return _json_error("query is required")
        index = self.agent.index
        if not index.available():
            return web.json_response(
                {"error": "recall index not ready", "status": index.status()},
                status=503,
            )
        results = await index.search(
            query=query,
            scope=body.get("scope", "both"),
            top_k=int(body.get("top_k", 8)),
            date_from=body.get("date_from"),
            date_to=body.get("date_to"),
        )
        return web.json_response({"results": results})

    async def heartbeat_trigger(self, request: web.Request) -> web.Response:
        if self.heartbeat is None:
            return _json_error("heartbeat unavailable", 500)
        asyncio.create_task(self.heartbeat.trigger_now())
        return web.json_response({"ok": True})

    # ── status, logs, update, restart ────────────────────────────

    async def status_full(self, request: web.Request) -> web.Response:
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.instance import get_instance, load_pending_update

        instance = get_instance()
        creds = load_credentials()

        providers_auth = {}
        for name in ("anthropic", "openai", "gemini", "openrouter"):
            pc = getattr(creds.providers, name, None)
            providers_auth[name] = {
                "api_key": bool(pc and pc.api_key),
                "oauth": bool(pc and pc.oauth_key),
            }
        try:
            from ragnarbot.auth import gemini_oauth, openai_oauth
            providers_auth["gemini"]["oauth"] = gemini_oauth.is_authenticated()
            providers_auth["openai"]["oauth"] = openai_oauth.is_authenticated()
        except Exception:
            pass

        daemon: dict[str, Any] = {"status": "unsupported"}
        try:
            from ragnarbot.daemon.resolve import get_manager
            info = get_manager().status()
            daemon = {
                "status": info.status.value if hasattr(info.status, "value") else str(info.status),
                "pid": info.pid,
                "log_path": str(info.log_path) if info.log_path else None,
            }
        except Exception as e:
            daemon["detail"] = str(e)

        cron_jobs = self.cron.list_jobs(include_disabled=True) if self.cron else []
        next_runs = [j.state.next_run_at_ms for j in cron_jobs
                     if j.enabled and j.state.next_run_at_ms]

        return web.json_response({
            "version": __version__,
            "profile": instance.profile,
            "model": self.agent.model,
            "fallback_model": self.agent._fallback_model,
            "workspace": str(self.agent.workspace),
            "daemon": daemon,
            "providers": providers_auth,
            "pending_update": load_pending_update(),
            "channels": {
                "telegram": {"enabled": self.config.channels.telegram.enabled},
                "web": {"clients": self.server.channel.client_count},
            },
            "heartbeat": {
                "enabled": self.config.heartbeat.enabled,
                "interval_m": self.config.heartbeat.interval_m,
            },
            "cron": {"jobs": len(cron_jobs), "next_run_at_ms": min(next_runs, default=None)},
            "hooks": {
                "enabled": self.config.hooks.enabled,
                "port": self.config.hooks.port,
                "count": len(self.hooks.list_hooks(include_disabled=True)) if self.hooks else 0,
            },
            "recall": {"status": self.agent.index.status(), "ready": self.agent.index.available()},
            "transcription": self.config.transcription.provider,
            "notifications_unread": self.notifications.unread_count() if self.notifications else 0,
        })

    async def logs_tail(self, request: web.Request) -> web.Response:
        from ragnarbot.instance import get_instance

        lines = min(int(request.query.get("lines", "200")), 2000)
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
        return web.json_response({"detail": result})

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
            limit=int(request.query.get("limit", "50")),
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
        ids = body.get("ids")
        self.notifications.mark_read(None if body.get("all") else ids)
        return web.json_response({"ok": True, "unread": self.notifications.unread_count()})

    async def usage(self, request: web.Request) -> web.Response:
        range_key = request.query.get("range", "week")
        days = {"day": 1, "week": 7, "month": 30}.get(range_key, 7)
        cutoff = datetime.now() - timedelta(days=days)
        path = (self.server.data_dir or Path(".")) / "web" / "usage.jsonl"

        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "turns": 0}
        by_model: dict[str, dict[str, int]] = {}
        by_source: dict[str, dict[str, int]] = {}
        by_day: dict[str, dict[str, int]] = {}
        if path.is_file():
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        ts = datetime.fromisoformat(rec["ts"])
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    if ts < cutoff:
                        continue
                    inp = rec.get("input_tokens", 0) or 0
                    out = rec.get("output_tokens", 0) or 0
                    totals["input_tokens"] += inp
                    totals["output_tokens"] += out
                    totals["cache_read_tokens"] += rec.get("cache_read_tokens", 0) or 0
                    totals["turns"] += 1
                    for key, bucket in (
                        (rec.get("model", "unknown"), by_model),
                        (rec.get("source", "unknown"), by_source),
                        (ts.strftime("%Y-%m-%d"), by_day),
                    ):
                        b = bucket.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "turns": 0})
                        b["input_tokens"] += inp
                        b["output_tokens"] += out
                        b["turns"] += 1
        return web.json_response({
            "range": range_key,
            "totals": totals,
            "by_model": by_model,
            "by_source": by_source,
            "by_day": by_day,
        })


def _safe_name(name: str) -> str | None:
    """Validate a skill/agent directory name (no path tricks)."""
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name or ""):
        return name
    return None


def _validate_schedule(schedule: Any) -> str | None:
    """Validate a CronSchedule; returns an error message or None."""
    if schedule.kind == "at":
        if not schedule.at_ms:
            return "at_ms is required for one-time schedules"
    elif schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms < 1000:
            return "every_ms must be at least 1000"
    elif schedule.kind == "cron":
        if not schedule.expr:
            return "expr is required for cron schedules"
        try:
            import croniter
            croniter.croniter(schedule.expr)
        except Exception as e:
            return f"invalid cron expression: {e}"
        if schedule.tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(schedule.tz)
            except Exception:
                return f"unknown timezone: {schedule.tz}"
    else:
        return f"unknown schedule kind: {schedule.kind}"
    return None
