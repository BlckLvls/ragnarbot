"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from ragnarbot.instance import resolve_workspace_path, workspace_config_value


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
        default_factory=workspace_config_value,
        json_schema_extra={"reload": "cold", "label": "Workspace directory path"},
    )
    model: str = Field(
        default="anthropic/claude-opus-4-8",
        json_schema_extra={"reload": "warm", "label": "LLM model identifier (provider/model)"},
    )
    reasoning_level: str = Field(
        default="medium",
        pattern="^(off|low|medium|high|ultra|max)$",
        json_schema_extra={"reload": "hot", "label": "Unified reasoning level"},
    )
    lightning_mode: bool = Field(
        default=False,
        json_schema_extra={
            "reload": "hot",
            "label": "Enable Lightning Mode (supported OpenAI models only; doubles token pricing)",
        },
    )
    max_context_tokens: int = Field(
        default=200_000,
        json_schema_extra={"reload": "hot", "label": "Maximum context window tokens"},
    )
    auth_method: str = Field(
        default="api_key",
        pattern="^(api_key|oauth)$",
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
    trace_mode: bool = Field(
        default=False,
        json_schema_extra={"reload": "hot", "label": "Show tool calls in chat during execution"},
    )
    steering_enabled: bool = Field(
        default=True,
        json_schema_extra={"reload": "hot", "label": "Inject same-session messages into active runs"},
    )
    experimental_soul: bool = Field(
        default=False,
        json_schema_extra={"reload": "hot", "label": "Use experimental soul prompt"},
    )


class FallbackConfig(BaseModel):
    """Fallback model configuration."""
    model: str | None = Field(
        default=None,
        json_schema_extra={"reload": "warm", "label": "Fallback model identifier"},
    )
    auth_method: str = Field(
        default="api_key",
        pattern="^(api_key|oauth)$",
        json_schema_extra={"reload": "warm", "label": "Fallback auth method"},
    )
    consecutive_failures_threshold: int = Field(
        default=3,
        json_schema_extra={"reload": "hot", "label": "Failures before fallback mode"},
    )
    recovery_probe_interval: int = Field(
        default=60,
        json_schema_extra={"reload": "hot", "label": "Seconds between primary recovery probes"},
    )


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


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
    safety_guard: bool = Field(
        default=True,
        json_schema_extra={"reload": "hot", "label": "Enable shell command safety guard"},
    )


class SearchToolConfig(BaseModel):
    """grep / glob search tools configuration."""
    backend: str = Field(
        default="auto",
        pattern="^(auto|ripgrep|python)$",
        json_schema_extra={"reload": "hot", "label": "Search backend (auto|ripgrep|python)"},
    )
    auto_install: bool = Field(
        default=True,
        json_schema_extra={"reload": "hot", "label": "Auto-download ripgrep when missing"},
    )
    max_matches: int = Field(
        default=200,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Max grep matches returned"},
    )
    max_results: int = Field(
        default=200,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Max glob results returned"},
    )
    max_output_chars: int = Field(
        default=20000,
        ge=1000,
        json_schema_extra={"reload": "hot", "label": "Max search output characters"},
    )
    timeout: int = Field(
        default=30,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Search timeout (seconds)"},
    )


class BrowserConfig(BaseModel):
    """Browser automation tool configuration."""
    idle_timeout: int = Field(
        default=600,
        json_schema_extra={"reload": "hot", "label": "Auto-close idle sessions (seconds)"},
    )
    headless: bool = Field(
        default=True,
        json_schema_extra={"reload": "hot", "label": "Run browser in headless mode"},
    )
    viewport_width: int = Field(
        default=1920,
        json_schema_extra={"reload": "hot", "label": "Browser viewport width (pixels)"},
    )
    viewport_height: int = Field(
        default=1080,
        json_schema_extra={"reload": "hot", "label": "Browser viewport height (pixels)"},
    )


class RecallToolConfig(BaseModel):
    """Hybrid (vector + BM25) recall search over memory files and chats."""
    enabled: bool = Field(
        default=True,
        json_schema_extra={"reload": "warm", "label": "Enable the recall search tool + background indexing"},
    )
    auto_install: bool = Field(
        default=True,
        json_schema_extra={"reload": "warm", "label": "Auto-download the embedding model and sqlite-vec extension"},
    )
    quant: str = Field(
        default="q4",
        pattern="^(q4|q8|fp16|fp32)$",
        json_schema_extra={"reload": "warm", "label": "EmbeddingGemma quant (q4|q8|fp16|fp32)"},
    )
    embed_rev: str = Field(
        default="5090578d9565bb06545b4552f76e6bc2c93e4a66",
        json_schema_extra={"reload": "warm", "label": "Pinned onnx-community model revision (commit sha)"},
    )
    top_k: int = Field(
        default=8,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Default number of recall results"},
    )
    scope_default: str = Field(
        default="both",
        pattern="^(memory|chats|both)$",
        json_schema_extra={"reload": "hot", "label": "Default recall scope (memory|chats|both)"},
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Reciprocal Rank Fusion constant k"},
    )
    max_output_chars: int = Field(
        default=20000,
        ge=1000,
        json_schema_extra={"reload": "hot", "label": "Max characters in a recall result payload"},
    )


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    search: SearchToolConfig = Field(default_factory=SearchToolConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    recall: RecallToolConfig = Field(default_factory=RecallToolConfig)


class TranscriptionConfig(BaseModel):
    """Voice transcription configuration."""
    provider: str = Field(
        default="none",
        pattern="^(groq|elevenlabs|openai-gpt-4o-transcribe|openai-gpt-4o-mini-transcribe|none)$",
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


class HooksConfig(BaseModel):
    """Webhook hooks configuration."""
    enabled: bool = Field(
        default=True,
        json_schema_extra={"reload": "warm", "label": "Enable webhook hooks HTTP server"},
    )
    port: int = Field(
        default=18791,
        json_schema_extra={"reload": "warm", "label": "Hooks HTTP server port"},
    )
    rate_limit_per_hook: int = Field(
        default=60,
        ge=1,
        json_schema_extra={"reload": "hot", "label": "Max triggers per hook per minute"},
    )
    max_payload_bytes: int = Field(
        default=65536,
        ge=1024,
        json_schema_extra={"reload": "hot", "label": "Max hook payload size (bytes)"},
    )


class Config(BaseSettings):
    """Root configuration for ragnarbot."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return resolve_workspace_path(self.agents.defaults.workspace)

    class Config:
        env_prefix = "RAGNARBOT_"
        env_nested_delimiter = "__"
