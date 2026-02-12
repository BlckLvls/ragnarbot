"""Update tool for checking, viewing changelogs, and self-updating ragnarbot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

import ragnarbot
from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ragnarbot.agent.loop import AgentLoop

GITHUB_REPO = "BlckLvls/ragnarbot"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
UPDATE_MARKER = Path.home() / ".ragnarbot" / ".update_marker"


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

    async def _action_check(self) -> str:
        """Check if a newer version is available."""
        current = ragnarbot.__version__
        try:
            latest = await self._get_latest_version()
        except Exception as e:
            return json.dumps({"error": f"Failed to check for updates: {e}"})

        update_available = _parse_version(latest) > _parse_version(current)
        return json.dumps({
            "current_version": current,
            "latest_version": latest,
            "update_available": update_available,
        })

    async def _action_changelog(self, version: str | None = None) -> str:
        """Fetch the GitHub release notes for a version."""
        try:
            if version:
                target = version.lstrip("v")
            else:
                target = await self._get_latest_version()

            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    f"{GITHUB_API}/releases/tags/v{target}"
                )
                r.raise_for_status()
                data = r.json()

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

        # Try uv first, fall back to pip
        try:
            proc = await asyncio.create_subprocess_exec(
                "uv", "tool", "upgrade", "ragnarbot-ai",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode().strip())
        except FileNotFoundError:
            # uv not available, try pip
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip", "install", "--upgrade", "ragnarbot-ai",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(stderr.decode().strip())
            except FileNotFoundError:
                return json.dumps({
                    "error": "Neither uv nor pip found. Cannot upgrade.",
                })
            except RuntimeError as e:
                return json.dumps({"error": f"pip upgrade failed: {e}"})
        except RuntimeError as e:
            return json.dumps({"error": f"uv upgrade failed: {e}"})

        # Write update marker for post-restart notification
        UPDATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_MARKER.write_text(json.dumps({
            "channel": self._channel,
            "chat_id": self._chat_id,
            "old_version": current,
            "new_version": latest,
        }))

        self._agent.request_restart()
        return json.dumps({
            "status": "updating",
            "old_version": current,
            "new_version": latest,
        })
