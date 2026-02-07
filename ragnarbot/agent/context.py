"""Context builder for assembling agent prompts."""

import base64
import mimetypes
from pathlib import Path
from typing import Any

from ragnarbot.agent.memory import MemoryStore
from ragnarbot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
    
    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        session_metadata: dict | None = None,
        channel: str | None = None,
    ) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
        
        Returns:
            Complete system prompt.
        """
        parts = []
        
        # Core identity
        parts.append(self._get_identity())
        
        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)
        
        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")
        
        # Telegram context
        if channel == "telegram" and session_metadata and "user_data" in session_metadata:
            from ragnarbot.prompts.telegram import TELEGRAM_CONTEXT

            user_data = session_metadata["user_data"]
            full_name = " ".join(
                filter(None, [user_data.get("first_name"), user_data.get("last_name")])
            )
            telegram_section = TELEGRAM_CONTEXT.format(
                full_name=full_name or "Unknown",
                username=user_data.get("username") or "N/A",
                user_id=user_data.get("user_id") or "N/A",
            )
            parts.append(telegram_section)

        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        
        # 2. Available skills: only show summary (agent uses file_read to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the file_read tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")
        
        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        workspace_path = str(self.workspace.expanduser().resolve())
        
        return f"""# ragnarbot 🤖

You are ragnarbot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now}

## Workspace
Your workspace is at: {workspace_path}
- Memory files: {workspace_path}/memory/MEMORY.md
- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel.
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. When using tools, explain what you're doing.
When remembering something, write to {workspace_path}/memory/MEMORY.md"""
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
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
            current_message: The new user message.
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
            h_refs = h_msg.pop("media_refs", None)
            if h_refs and session_key and h_msg.get("role") == "user":
                h_msg["content"] = self._build_user_content(
                    h_msg.get("content", ""), media_refs=h_refs,
                    session_key=session_key, media_base=media_base,
                )
            messages.append(h_msg)

        # Current message (with optional image attachments)
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
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
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
