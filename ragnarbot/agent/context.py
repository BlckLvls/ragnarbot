"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from ragnarbot.agent.memory import MemoryStore
from ragnarbot.agent.skills import SkillsLoader

DEFAULTS_DIR = Path(__file__).parent.parent / "workspace_defaults"
BUILTIN_DIR = Path(__file__).parent.parent / "builtin"


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles built-in files, user workspace files, memory, skills, and
    conversation history into a coherent prompt for the LLM.
    """

    BUILTIN_FILES = ["SOUL.md", "AGENTS.md", "BUILTIN_TOOLS.md"]
    BOOTSTRAP_FILES = ["IDENTITY.md", "USER.md", "TOOLS.md"]

    def __init__(self, workspace: Path, heartbeat_interval_m: int = 30):
        self.workspace = workspace
        self.heartbeat_interval_m = heartbeat_interval_m
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._ensure_bootstrap_files()

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        session_metadata: dict | None = None,
        channel: str | None = None,
    ) -> str:
        """
        Build the system prompt from built-in files, workspace files, memory, and skills.

        Args:
            skill_names: Optional list of skills to include.

        Returns:
            Complete system prompt.
        """
        parts = []

        # 1. Minimal identity header (dynamic values)
        parts.append(self._get_identity())

        # 2. Built-in files (raw, no extra headers)
        builtin = self._load_builtin_files()
        if builtin:
            parts.append(builtin)

        # 3. Built-in Telegram (conditional, raw)
        if channel == "telegram" and session_metadata and "user_data" in session_metadata:
            telegram = self._load_builtin_telegram(session_metadata["user_data"])
            if telegram:
                parts.append(telegram)

        # 3b. Cron isolated mode (conditional)
        if session_metadata and session_metadata.get("cron_isolated"):
            cron_ctx = session_metadata["cron_isolated"]
            cron_isolated = self._load_builtin_cron_isolated(cron_ctx)
            if cron_isolated:
                parts.append(cron_isolated)

        # 3c. Heartbeat isolated mode (conditional)
        if session_metadata and session_metadata.get("heartbeat_isolated"):
            hb_ctx = session_metadata["heartbeat_isolated"]
            hb_isolated = self._load_builtin_heartbeat_isolated(hb_ctx)
            if hb_isolated:
                parts.append(hb_isolated)

        # 4. Bootstrap protocol (first-run only, self-deleting)
        bootstrap_protocol = self.workspace / "BOOTSTRAP.md"
        if bootstrap_protocol.exists():
            content = bootstrap_protocol.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

        # 5. User-editable files (with filename + path headers)
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # 6. Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # 7. Skills - progressive loading
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the file_read tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the minimal identity header with dynamic values."""
        import time
        tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
        utc_offset = time.strftime("%z")
        workspace_path = str(self.workspace.expanduser().resolve())

        return f"""# ragnarbot

**Timezone:** {tz_name} (UTC{utc_offset[:3]}:{utc_offset[3:]})
**Workspace:** {workspace_path}"""

    def _load_builtin_files(self) -> str:
        """Load built-in developer-controlled files, applying placeholders."""
        import time
        tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
        utc_offset = time.strftime("%z")
        workspace_path = str(self.workspace.expanduser().resolve())
        timezone = f"{tz_name} (UTC{utc_offset[:3]}:{utc_offset[3:]})"

        parts = []
        for filename in self.BUILTIN_FILES:
            file_path = BUILTIN_DIR / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                content = content.format(
                    workspace_path=workspace_path,
                    timezone=timezone,
                    heartbeat_interval_m=self.heartbeat_interval_m,
                )
                parts.append(content)
        return "\n\n---\n\n".join(parts) if parts else ""

    def _load_builtin_telegram(self, user_data: dict) -> str:
        """Load the built-in Telegram context file with user placeholders."""
        from telegram.constants import ReactionEmoji

        file_path = BUILTIN_DIR / "TELEGRAM.md"
        if not file_path.exists():
            return ""
        content = file_path.read_text(encoding="utf-8")
        full_name = " ".join(
            filter(None, [user_data.get("first_name"), user_data.get("last_name")])
        )
        reaction_emojis = " ".join(e.value for e in ReactionEmoji)
        return content.format(
            full_name=full_name or "Unknown",
            username=user_data.get("username") or "N/A",
            user_id=user_data.get("user_id") or "N/A",
            reaction_emojis=reaction_emojis,
        )

    def _load_builtin_cron_isolated(self, cron_ctx: dict) -> str:
        """Load the cron isolated mode system prompt with placeholders."""
        import time as _time
        file_path = BUILTIN_DIR / "CRON_ISOLATED.md"
        if not file_path.exists():
            return ""
        content = file_path.read_text(encoding="utf-8")
        return content.format(
            job_name=cron_ctx.get("job_name", "Unknown"),
            schedule_desc=cron_ctx.get("schedule_desc", "Unknown"),
            current_time=_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            task_message=cron_ctx.get("task_message", ""),
        )

    def _load_builtin_heartbeat_isolated(self, hb_ctx: dict) -> str:
        """Load the heartbeat isolated mode system prompt with placeholders."""
        import time as _time
        file_path = BUILTIN_DIR / "HEARTBEAT_ISOLATED.md"
        if not file_path.exists():
            return ""
        content = file_path.read_text(encoding="utf-8")
        return content.format(
            current_time=_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            tasks_summary=hb_ctx.get("tasks_summary", "No tasks."),
        )

    def _load_bootstrap_files(self) -> str:
        """Load user-editable workspace files with filename and path headers."""
        workspace_path = str(self.workspace.expanduser().resolve())
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                header = f"## {filename}\n> Path: {workspace_path}/{filename}"
                parts.append(f"{header}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _ensure_bootstrap_files(self) -> None:
        """Copy default workspace files from workspace_defaults/ if missing or empty."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        bootstrap_done = self.workspace / ".bootstrap_done"

        for default_file in DEFAULTS_DIR.rglob("*"):
            if not default_file.is_file():
                continue
            rel = default_file.relative_to(DEFAULTS_DIR)

            # BOOTSTRAP.md: only create on first run (never re-create after completion)
            if rel.name == "BOOTSTRAP.md" and bootstrap_done.exists():
                continue

            target = self.workspace / rel
            if not target.exists() or not target.read_text(encoding="utf-8").strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(default_file, target)

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str | None = None,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        media_refs: list[dict[str, str]] | None = None,
        session_key: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        session_metadata: dict | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message. When None, only system
                prompt and history are included (useful for token counting).
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media (voice/audio).
            media_refs: Optional photo references saved by MediaManager.
            session_key: Session key for resolving media_refs paths.
            channel: Current channel (e.g. telegram).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages: list[dict[str, Any]] = []

        # System prompt
        system_prompt = self.build_system_prompt(
            skill_names, session_metadata=session_metadata, channel=channel
        )
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History (resolve media_refs for past photos)
        media_base = self._media_base_dir()
        for h_msg in history:
            h_refs = h_msg.get("media_refs")
            if h_refs and session_key and h_msg.get("role") == "user":
                h_msg["content"] = self._build_user_content(
                    h_msg.get("content", ""), media_refs=h_refs,
                    session_key=session_key, media_base=media_base,
                )
            messages.append(h_msg)

        # Current message (with optional image attachments)
        if current_message is not None:
            user_content = self._build_user_content(
                current_message, media=media, media_refs=media_refs,
                session_key=session_key, media_base=media_base,
            )
            messages.append({"role": "user", "content": user_content})

        return messages

    def _media_base_dir(self) -> Path:
        """Return the base media directory."""
        return Path.home() / ".ragnarbot" / "media"

    def _build_user_content(
        self,
        text: str,
        media: list[str] | None = None,
        media_refs: list[dict[str, str]] | None = None,
        session_key: str | None = None,
        media_base: Path | None = None,
    ) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images.

        Supports two image sources:
        - ``media``: legacy file paths (voice/audio or old-style images)
        - ``media_refs``: new per-session photo references from MediaManager
        """
        images: list[dict[str, Any]] = []

        # Legacy: base64-encode from file paths
        if media:
            for path in media:
                p = Path(path)
                mime, _ = mimetypes.guess_type(path)
                if not p.is_file() or not mime or not mime.startswith("image/"):
                    continue
                b64 = base64.b64encode(p.read_bytes()).decode()
                images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        # New: lazy base64-encode from media_refs
        if media_refs and session_key and media_base:
            for ref in media_refs:
                if ref.get("type") != "photo":
                    continue
                photo_path = media_base / session_key / "photos" / ref["filename"]
                if not photo_path.is_file():
                    continue
                mime, _ = mimetypes.guess_type(str(photo_path))
                mime = mime or "image/jpeg"
                b64 = base64.b64encode(photo_path.read_bytes()).decode()
                images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def build_user_message(
        self,
        content: str,
        media: list[str] | None = None,
        media_refs: list[dict[str, str]] | None = None,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        """Build a standalone user message dict (for appending extra batch items).

        Args:
            content: The user message text (already prefixed).
            media: Optional legacy media file paths.
            media_refs: Optional photo references from MediaManager.
            session_key: Session key for resolving media_refs paths.

        Returns:
            A dict with role="user" and the assembled content.
        """
        user_content = self._build_user_content(
            content, media=media, media_refs=media_refs,
            session_key=session_key, media_base=self._media_base_dir(),
        )
        return {"role": "user", "content": user_content}

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        messages.append(msg)
        return messages
