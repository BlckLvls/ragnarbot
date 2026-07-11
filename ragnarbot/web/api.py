"""REST API for the web console modules: config, cron, hooks, agents, files, status."""

import json
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
_WORKSPACE_HIDDEN_DIRS = frozenset({"agents", "__pycache__", "node_modules"})
_WORKSPACE_SENSITIVE_STEMS = ("auth", "credential", "secret", "token", "private-key", "private_key")
_WORKSPACE_MAX_EDIT_BYTES = 2 * 1024 * 1024


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
        # agents: live runs only
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
        # workspace files & skills
        r.add_get("/api/workspace/tree", self.workspace_tree)
        r.add_get("/api/workspace/file", self.workspace_file_get)
        r.add_put("/api/workspace/file", self.workspace_file_put)
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
    def _workspace_file_is_exposed(rel: Path) -> bool:
        """Keep Files focused on safe text documents and source files."""
        if not rel.parts or any(part.startswith(".") for part in rel.parts):
            return False
        if any(part in _WORKSPACE_HIDDEN_DIRS for part in rel.parts[:-1]):
            return False
        stem = rel.stem.lower()
        if "sanitized" not in stem and any(
            stem == prefix or stem.startswith(f"{prefix}-") or stem.startswith(f"{prefix}_")
            for prefix in _WORKSPACE_SENSITIVE_STEMS
        ):
            return False
        return rel.suffix.lower() in _WORKSPACE_TEXT_SUFFIXES

    def _workspace_path(self, raw: str, *, exposed_file: bool = False) -> Path:
        path = (self.workspace / raw).resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise web.HTTPForbidden(reason="path outside workspace")
        rel = path.relative_to(self.workspace.resolve())
        if rel.parts and rel.parts[0] == "agents":
            raise web.HTTPForbidden(reason="agent definitions are not exposed in the web console")
        if exposed_file and not self._workspace_file_is_exposed(rel):
            raise web.HTTPForbidden(reason="file is not exposed in the web console")
        return path

    async def workspace_tree(self, request: web.Request) -> web.Response:
        root = self.workspace.resolve()
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if (
                not path.is_file()
                or path.is_symlink()
                or len(rel.parts) > 6
                or not self._workspace_file_is_exposed(rel)
                or path.stat().st_size > _WORKSPACE_MAX_EDIT_BYTES
            ):
                continue
            files.append(path)

        directories = {
            parent
            for path in files
            for parent in path.relative_to(root).parents
            if parent != Path(".")
        }
        entries = [
            {"path": str(rel), "dir": True, "size": None}
            for rel in sorted(directories, key=str)
        ]
        entries.extend({
            "path": str(path.relative_to(root)),
            "dir": False,
            "size": path.stat().st_size,
        } for path in files)
        entries.sort(key=lambda entry: entry["path"])
        return web.json_response(entries)

    async def workspace_file_get(self, request: web.Request) -> web.Response:
        path = self._workspace_path(request.query.get("path", ""), exposed_file=True)
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
        path = self._workspace_path(raw, exposed_file=True)
        if not path.is_file():
            raise web.HTTPNotFound()
        content = body.get("content", "")
        if not isinstance(content, str):
            return _json_error("content must be a string")
        if len(content.encode("utf-8")) > _WORKSPACE_MAX_EDIT_BYTES:
            return _json_error("file too large to edit", 413)
        path.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})

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
