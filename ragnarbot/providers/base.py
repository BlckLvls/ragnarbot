"""Base LLM provider interface."""

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_TOKENS = 32_000
# Synchronous-API output ceiling for current OpenAI GPT-5.x and Anthropic Claude 4.x
# models (both cap a single response at 128k tokens). Used as the default response
# budget for those providers so long generations are not truncated early.
MAX_OUTPUT_TOKENS = 128_000

_AUTHORIZATION_VALUE_RE = re.compile(
    r"(?i)(\bauthorization\b['\"]?\s*[:=]\s*['\"]?)"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,'\"}\]]+"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[^\s,'\"}\]]+")
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _redact_provider_secrets(message: str) -> str:
    """Redact common provider credential shapes from diagnostic text."""
    message = _AUTHORIZATION_VALUE_RE.sub(r"\1[REDACTED]", message)
    message = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", message)
    return _SK_TOKEN_RE.sub("sk-[REDACTED]", message)


def format_provider_exception(exc: BaseException) -> str:
    """Return a compact, non-empty exception summary including useful causes.

    Some transport exceptions have an empty string representation, while SDKs
    often wrap the actionable network detail in ``__cause__``. Keep the
    user-facing error bounded and avoid ``repr()``, which may include verbose
    request objects.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and len(parts) < 4 and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        try:
            message = _redact_provider_secrets(" ".join(str(current).split()))
        except Exception:
            message = ""
        if len(message) > 300:
            message = f"{message[:297]}..."
        parts.append(f"{name}: {message}" if message else name)

        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause

    return " <- ".join(parts) or type(exc).__name__


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


ToolRunner = Callable[[ToolCallRequest], Awaitable[Any]]
ToolCallHandler = Callable[[ToolCallRequest], Awaitable[None]]
TextDeltaHandler = Callable[[str], Awaitable[None]]
SteeringMessageProvider = Callable[[], Awaitable[list[dict[str, Any]]]]


@dataclass
class ExecutedToolCall:
    """A tool call that was executed inside the provider transport."""

    id: str
    name: str
    arguments: dict[str, Any]
    result: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    assistant_content: str | None = None


@dataclass
class ConsumedSteeringMessage:
    """A live steering message consumed inside a provider-managed turn."""

    after_executed_tool_calls: int
    user_message: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    executed_tool_calls: list[ExecutedToolCall] = field(default_factory=list)
    consumed_steering_messages: list[ConsumedSteeringMessage] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(
        self,
        api_key: str | None = None,
        oauth_token: str | None = None,
    ):
        self.api_key = api_key
        self.oauth_token = oauth_token

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_level: str | None = None,
        lightning_mode: bool | None = None,
        session_key: str | None = None,
        tool_runner: ToolRunner | None = None,
        tool_call_handler: ToolCallHandler | None = None,
        text_delta_handler: TextDeltaHandler | None = None,
        steering_message_provider: SteeringMessageProvider | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response (defaults to DEFAULT_MAX_TOKENS).
            temperature: Sampling temperature (omitted if None — uses API default).
            reasoning_level: Unified reasoning level (off/low/medium/high/ultra/max).
            lightning_mode: Whether OpenAI Lightning Mode is enabled for this request.
            session_key: Stable conversation identifier for stateful transports.
            tool_runner: Optional host-side tool executor for transports that
                invoke tool calls interactively.
            tool_call_handler: Optional callback for transports that want to
                surface tool calls before the turn completes.
            text_delta_handler: Optional callback for incremental text deltas.
            steering_message_provider: Optional callback for transports that
                can inject same-session steering messages into an in-flight turn.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass

    async def aclose(self) -> None:
        """Release any provider-managed resources."""
        return None
