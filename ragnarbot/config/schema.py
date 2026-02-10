"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


OAUTH_SUPPORTED_PROVIDERS = {"anthropic"}


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = "~/.ragnarbot/workspace"
    model: str = "anthropic/claude-opus-4-6"
    max_tokens: int = 8192
    temperature: float = 0.7
    max_context_tokens: int = 200_000
    auth_method: str = "api_key"
    stream_steps: bool = True  # Send intermediate messages to user during tool-call loops
    debounce_seconds: float = 0.5  # Batch rapid-fire messages into a single LLM turn
    context_mode: str = Field(default="normal", pattern="^(eco|normal|full)$")


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)



class DaemonConfig(BaseModel):
    """Daemon auto-start configuration."""
    enabled: bool = False


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    timeout: int = 60
    restrict_to_workspace: bool = False  # If true, block commands accessing paths outside workspace


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)


class TranscriptionConfig(BaseModel):
    """Voice transcription configuration."""
    provider: str = Field(default="none", pattern="^(groq|elevenlabs|none)$")


class Config(BaseSettings):
    """Root configuration for ragnarbot."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    class Config:
        env_prefix = "RAGNARBOT_"
        env_nested_delimiter = "__"
