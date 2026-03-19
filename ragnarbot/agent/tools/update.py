"""Update tool for checking, viewing changelogs, and self-updating ragnarbot."""

from __future__ import annotations

import asyncio
import json
import signal
from typing import TYPE_CHECKING, Any

import httpx

import ragnarbot
from ragnarbot.agent.tools.base import Tool
from ragnarbot.instance import (
    clear_pending_update,
    ensure_instance_root,
    get_instance,
    get_live_gateway_pid,
    instance_profiles_on_disk,
    last_active_chat,
    load_pending_update,
    resolve_active_profile,
    save_pending_update,
    signal_live_gateway,
)

if TYPE_CHECKING:
    from ragnarbot.agent.loop import AgentLoop

GITHUB_REPO = "BlckLvls/ragnarbot"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"


def get_update_marker_path():
    return ensure_instance_root().update_marker_path


def _parse_version(ver: str) -> tuple[int, ...]:
    """Strip optional 'v' prefix and parse version into comparable tuple."""
    return tuple(int(x) for x in ver.lstrip("v").split("."))


class UpdateTool(Tool):
    """Tool to check for updates, view changelogs, and self-update ragnarbot."""

    name = "update"
    description = (
        "Check for ragnarbot updates, view release notes, "
        "and self-update to the latest release. "
        "Actions: check (compare current vs latest), "
        "changelog (view release notes for a version), "
        "update (upgrade and restart)."
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["check", "changelog", "update"],
                "description": "Action to perform",
            },
            "version": {
                "type": "string",
                "description": "Target version for changelog (e.g. '0.4.0'). Optional.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, agent: AgentLoop):
        self._agent = agent
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for post-update notification."""
        self._channel = channel
        self._chat_id = chat_id

    async def execute(
        self,
        action: str,
        version: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "check":
            return await self._action_check()
        elif action == "changelog":
            return await self._action_changelog(version)
        elif action == "update":
            return await self._action_update()
        return f"Unknown action: {action}"

    async def _get_latest_version(self) -> str:
        """Fetch the latest version tag from GitHub."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{GITHUB_API}/tags", params={"per_page": 100})
            r.raise_for_status()
            tags = r.json()

        version_tags = [t["name"] for t in tags if t["name"].startswith("v")]
        if not version_tags:
            raise ValueError("No version tags found")

        version_tags.sort(key=_parse_version)
        return version_tags[-1].lstrip("v")

    async def _get_release(self, version: str) -> dict[str, Any]:
        """Fetch GitHub release metadata for a concrete version."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{GITHUB_API}/releases/tags/v{version}")
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _summarize_release_body(body: str) -> tuple[str, str]:
        """Return a short summary line and truncated body excerpt."""
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        summary = "Release notes available."
        for line in lines:
            cleaned = line.lstrip("#*- ").strip()
            if cleaned:
                summary = cleaned[:160]
                break
        excerpt = body.strip()[:1200]
        return summary, excerpt

    def _write_update_marker(
        self, current: str, latest: str, release: dict[str, Any],
    ) -> None:
        marker_path = get_update_marker_path()
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({
            "channel": self._channel,
            "chat_id": self._chat_id,
            "old_version": current,
            "new_version": latest,
            "changelog_url": release.get("html_url", ""),
        }))

    def _mark_other_profiles_pending(
        self, current: str, latest: str, release: dict[str, Any],
    ) -> list[str]:
        """Persist pending-update markers for every other known profile."""
        summary, excerpt = self._summarize_release_body(release.get("body", ""))
        payload_base = {
            "old_version": current,
            "new_version": latest,
            "summary": summary,
            "body_excerpt": excerpt,
            "changelog_url": release.get("html_url", ""),
            "release_name": release.get("name", f"v{latest}"),
            "published_at": release.get("published_at", ""),
        }
        current_profile = resolve_active_profile()
        notified: list[str] = []

        for profile in instance_profiles_on_disk():
            if profile == current_profile:
                continue
            info = get_instance(profile)
            if not info.data_root.exists():
                continue

            target_channel, target_chat_id = last_active_chat(profile)
            live_pid = get_live_gateway_pid(profile)
            payload = {
                **payload_base,
                "requires_restart": live_pid is not None,
            }
            if target_channel and target_chat_id:
                payload["target_channel"] = target_channel
                payload["target_chat_id"] = target_chat_id

            save_pending_update(payload, profile)
            notified.append(profile)

            if live_pid is not None:
                signal_live_gateway(signal.SIGUSR2, profile)

        return notified

    async def _action_check(self) -> str:
        """Check if a newer version is available."""
        current = ragnarbot.__version__
        pending = load_pending_update()
        try:
            latest = await self._get_latest_version()
        except Exception as e:
            return json.dumps({"error": f"Failed to check for updates: {e}"})

        update_available = _parse_version(latest) > _parse_version(current)
        return json.dumps({
            "current_version": current,
            "latest_version": latest,
            "update_available": update_available,
            "pending_update": pending,
        })

    async def _action_changelog(self, version: str | None = None) -> str:
        """Fetch the GitHub release notes for a version."""
        try:
            if version:
                target = version.lstrip("v")
            else:
                target = await self._get_latest_version()

            data = await self._get_release(target)

            return json.dumps({
                "version": target,
                "name": data.get("name", f"v{target}"),
                "body": data.get("body", ""),
                "url": data.get("html_url", ""),
                "published_at": data.get("published_at", ""),
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return json.dumps({
                    "error": f"No release found for v{target}",
                    "version": target,
                })
            return json.dumps({"error": f"Failed to fetch release: {e}"})
        except Exception as e:
            return json.dumps({"error": f"Failed to fetch release: {e}"})

    async def _action_update(self) -> str:
        """Upgrade ragnarbot and schedule a restart."""
        current = ragnarbot.__version__
        try:
            latest = await self._get_latest_version()
        except Exception as e:
            return json.dumps({"error": f"Failed to check for updates: {e}"})

        if _parse_version(latest) <= _parse_version(current):
            return json.dumps({
                "status": "up_to_date",
                "current_version": current,
            })

        try:
            release = await self._get_release(latest)
        except Exception as e:
            return json.dumps({"error": f"Failed to fetch release notes: {e}"})

        # Try uv first, fall back to pip
        try:
            returncode, stdout, stderr = await self._run_subprocess(
                "uv", "tool", "upgrade", "ragnarbot-ai",
            )
            if returncode != 0:
                raise RuntimeError(stderr.decode().strip())
        except FileNotFoundError:
            # uv not available, try pip
            try:
                returncode, stdout, stderr = await self._run_subprocess(
                    "pip", "install", "--upgrade", "ragnarbot-ai",
                )
                if returncode != 0:
                    raise RuntimeError(stderr.decode().strip())
            except FileNotFoundError:
                return json.dumps({
                    "error": "Neither uv nor pip found. Cannot upgrade.",
                })
            except RuntimeError as e:
                return json.dumps({"error": f"pip upgrade failed: {e}"})
        except RuntimeError as e:
            return json.dumps({"error": f"uv upgrade failed: {e}"})

        clear_pending_update()
        self._write_update_marker(current, latest, release)
        notified_profiles = self._mark_other_profiles_pending(current, latest, release)

        self._agent.request_restart()
        return json.dumps({
            "status": "updating",
            "old_version": current,
            "new_version": latest,
            "notified_profiles": notified_profiles,
        })

    @staticmethod
    async def _run_subprocess(*argv: str) -> tuple[int, bytes, bytes]:
        """Run a subprocess and ensure it is killed on cancellation."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            raise
        return proc.returncode or 0, stdout, stderr
