"""Resolution helpers for OpenAI Lightning Mode."""

from __future__ import annotations

from dataclasses import dataclass

LIGHTNING_SUPPORTED_MODELS = frozenset({
    "openai/gpt-5.4",
})

LIGHTNING_WORKS_NOTE = "Works only with supported OpenAI API models."
LIGHTNING_COST_NOTE = "Uses OpenAI Priority processing and doubles token pricing (2x vs standard)."
LIGHTNING_UNSUPPORTED_NOTE = (
    "Currently has no effect for this model/auth setup. OpenAI OAuth and OpenRouter are not supported."
)


@dataclass(frozen=True)
class LightningResolution:
    """Resolved Lightning Mode behavior for a specific request target."""

    model: str
    auth_method: str
    enabled: bool
    supported: bool
    applies: bool
    service_tier: str | None = None


def resolve_lightning(
    model: str,
    auth_method: str,
    lightning_mode: bool | None,
) -> LightningResolution:
    """Resolve Lightning Mode support for a given model/auth pair."""
    normalized_model = _normalize_model_id(model)
    enabled = bool(lightning_mode)
    supported = auth_method == "api_key" and normalized_model in LIGHTNING_SUPPORTED_MODELS

    return LightningResolution(
        model=normalized_model,
        auth_method=auth_method,
        enabled=enabled,
        supported=supported,
        applies=enabled and supported,
        service_tier="priority" if enabled and supported else None,
    )


def _normalize_model_id(model: str) -> str:
    if model.startswith("openai/"):
        return model
    if model.startswith("anthropic/"):
        return model
    if model.startswith("gemini/"):
        return model
    if model.startswith("openrouter/"):
        return model
    if model.startswith("gpt-"):
        return f"openai/{model}"
    if model.startswith("claude-"):
        return f"anthropic/{model}"
    if model.startswith("gemini-"):
        return f"gemini/{model}"
    return model
