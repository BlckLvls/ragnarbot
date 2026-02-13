"""Heartbeat tool for managing periodic heartbeat tasks."""

import random
import string
from pathlib import Path
from typing import Any

from ragnarbot.agent.tools.base import Tool


def _generate_id() -> str:
    """Generate a 5-char random ID (mixed case letters + digits)."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=5))


def parse_blocks(content: str) -> list[dict]:
    """Parse heartbeat block format into a list of task dicts.

    Block format:
        ---
        [aB3kQ] Task description here
        ---

    Returns:
        [{"id": "aB3kQ", "message": "Task description here"}, ...]
    """
    blocks = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip() == "---":
            # Look for task line after separator
            i += 1
            if i < len(lines):
                task_line = lines[i].strip()
                if task_line.startswith("[") and "]" in task_line:
                    bracket_end = task_line.index("]")
                    task_id = task_line[1:bracket_end]
                    message = task_line[bracket_end + 1:].strip()
                    blocks.append({"id": task_id, "message": message})
        i += 1
    return blocks


def render_blocks(blocks: list[dict]) -> str:
    """Render task dicts back into heartbeat block format.

    Args:
        blocks: [{"id": "aB3kQ", "message": "Task description"}, ...]

    Returns:
        Formatted string with --- separators and [ID] prefixes.
    """
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        parts.append(f"---\n[{block['id']}] {block['message']}\n---")
    return "\n".join(parts) + "\n"


class HeartbeatTool(Tool):
    """Tool to manage periodic heartbeat tasks in HEARTBEAT.md."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    def set_workspace(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def _heartbeat_path(self) -> Path:
        if not self._workspace:
            raise RuntimeError("Workspace not set for HeartbeatTool")
        return self._workspace / "HEARTBEAT.md"

    @property
    def name(self) -> str:
        return "heartbeat"

    @property
    def description(self) -> str:
        return "Manage periodic heartbeat tasks. Actions: add, remove, edit, list."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "edit", "list"],
                    "description": "Action to perform",
                },
                "message": {
                    "type": "string",
                    "description": "Task description (for add/edit)",
                },
                "id": {
                    "type": "string",
                    "description": "Task ID (for remove/edit)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        id: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add(message)
        elif action == "remove":
            return self._remove(id)
        elif action == "edit":
            return self._edit(id, message)
        elif action == "list":
            return self._list()
        return f"Unknown action: {action}"

    def _read_blocks(self) -> tuple[str, list[dict]]:
        """Read file and parse blocks. Returns (raw_content, blocks)."""
        path = self._heartbeat_path
        if not path.exists():
            return "", []
        content = path.read_text(encoding="utf-8")
        return content, parse_blocks(content)

    def _write_blocks(self, blocks: list[dict], header: str = "") -> None:
        """Write blocks back to HEARTBEAT.md, preserving any leading comment."""
        path = self._heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = header + render_blocks(blocks)
        path.write_text(content, encoding="utf-8")

    def _get_header(self, raw_content: str) -> str:
        """Extract leading HTML comments from the file."""
        lines = raw_content.split("\n")
        header_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("<!--") or stripped.endswith("-->") or not stripped:
                header_lines.append(line)
            else:
                break
        header = "\n".join(header_lines)
        if header.strip():
            header = header.rstrip("\n") + "\n"
        return header

    def _add(self, message: str) -> str:
        if not message:
            return "Error: message is required for add"
        raw, blocks = self._read_blocks()
        task_id = _generate_id()
        blocks.append({"id": task_id, "message": message})
        header = self._get_header(raw)
        self._write_blocks(blocks, header)
        return f"Added task [{task_id}]: {message}"

    def _remove(self, task_id: str) -> str:
        if not task_id:
            return "Error: id is required for remove"
        raw, blocks = self._read_blocks()
        new_blocks = [b for b in blocks if b["id"] != task_id]
        if len(new_blocks) == len(blocks):
            return f"Task {task_id} not found"
        header = self._get_header(raw)
        self._write_blocks(new_blocks, header)
        return f"Removed task {task_id}"

    def _edit(self, task_id: str, message: str) -> str:
        if not task_id:
            return "Error: id is required for edit"
        if not message:
            return "Error: message is required for edit"
        raw, blocks = self._read_blocks()
        for block in blocks:
            if block["id"] == task_id:
                block["message"] = message
                header = self._get_header(raw)
                self._write_blocks(blocks, header)
                return f"Updated task [{task_id}]: {message}"
        return f"Task {task_id} not found"

    def _list(self) -> str:
        _, blocks = self._read_blocks()
        if not blocks:
            return "No heartbeat tasks."
        lines = []
        for b in blocks:
            lines.append(f"- [{b['id']}] {b['message']}")
        return "Heartbeat tasks:\n" + "\n".join(lines)
