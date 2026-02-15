"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = Field(
        default=False,
        json_schema_extra={"reload": "warm", "label": "Enable Telegram channel"},
    )
    allow_from: list[str] = Field(
        default_factory=list,
        json_schema_extra={"reload": "warm", "label": "Allowed Telegram user IDs or usernames"},
    )
    proxy: str | None = Field(
        default=None,
        json_schema_extra={"reload": "warm", "label": "HTTP/SOCKS5 proxy URL for Telegram"},
    )


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


OAUTH_SUPPORTED_PROVIDERS = {"anthropic", "gemini", "openai"}


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = Field(
        default="~/.ragnarbot/workspace",
        json_schema_extra={"reload": "cold", "label": "Workspace directory path"},
    )
    model: str = Field(
        default="anthropic/claude-opus-4-6",
        json_schema_extra={"reload": "warm", "label": "LLM model identifier (provider/model)"},
    )
    max_tokens: int = Field(
        default=16_000,
        json_schema_extra={"reload": "hot", "label": "Maximum tokens in LLM response"},
    )
    temperature: float = Field(
        default=0.7,
        json_schema_extra={"reload": "hot", "label": "LLM sampling temperature"},
    )
    max_context_tokens: int = Field(
        default=200_000,
        json_schema_extra={"reload": "hot", "label": "Maximum context window tokens"},
    )
    auth_method: str = Field(
        default="api_key",
        json_schema_extra={"reload": "warm", "label": "Authentication method (api_key or oauth)"},
    )
    stream_steps: bool = Field(
        default=True,
        json_schema_extra={"reload": "hot", "label": "Send intermediate messages during tool loops"},
    )
    debounce_seconds: float = Field(
        default=0.5,
        json_schema_extra={"reload": "hot", "label": "Batch rapid-fire messages delay (seconds)"},
    )
    context_mode: str = Field(
        default="normal",
        pattern="^(eco|normal|full)$",
        json_schema_extra={"reload": "hot", "label": "Context management mode"},
    )


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class DaemonConfig(BaseModel):
    """Daemon auto-start configuration."""
    enabled: bool = Field(
        default=False,
        json_schema_extra={"reload": "warm", "label": "Enable daemon auto-start"},
    )


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = Field(
        default="0.0.0.0",
        json_schema_extra={"reload": "warm", "label": "Gateway bind address"},
    )
    port: int = Field(
        default=18790,
        json_schema_extra={"reload": "warm", "label": "Gateway port number"},
    )


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    engine: str = Field(
        default="brave",
        pattern="^(brave|duckduckgo)$",
        json_schema_extra={"reload": "hot", "label": "Search engine backend"},
    )
    max_results: int = Field(
        default=10,
        json_schema_extra={"reload": "hot", "label": "Default number of search results"},
    )


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    timeout: int = Field(
        default=60,
        json_schema_extra={"reload": "hot", "label": "Shell command timeout (seconds)"},
    )
    restrict_to_workspace: bool = Field(
        default=False,
        json_schema_extra={"reload": "hot", "label": "Block commands outside workspace"},
    )


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)


class TranscriptionConfig(BaseModel):
    """Voice transcription configuration."""
    provider: str = Field(
        default="none",
        pattern="^(groq|elevenlabs|none)$",
        json_schema_extra={"reload": "warm", "label": "Voice transcription provider"},
    )


class HeartbeatConfig(BaseModel):
    """Heartbeat periodic task configuration."""
    enabled: bool = Field(
        default=True,
        json_schema_extra={"reload": "warm", "label": "Enable periodic heartbeat checks"},
    )
    interval_m: int = Field(
        default=30,
        ge=1,
        json_schema_extra={"reload": "warm", "label": "Heartbeat check interval (minutes)"},
    )


class Config(BaseSettings):
    """Root configuration for ragnarbot."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    class Config:
        env_prefix = "RAGNARBOT_"
        env_nested_delimiter = "__"
