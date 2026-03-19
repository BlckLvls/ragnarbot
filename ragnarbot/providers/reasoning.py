"""Unified reasoning-level resolution across exposed models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ReasoningLevel = Literal["off", "low", "medium", "high", "ultra"]
SUPPORTED_REASONING_LEVELS: tuple[ReasoningLevel, ...] = (
    "off",
    "low",
    "medium",
    "high",
    "ultra",
)


@dataclass(frozen=True)
class ReasoningResolution:
    """Resolved reasoning behavior for a specific model."""

    model: str
    stored_level: ReasoningLevel
    effective_level: ReasoningLevel
    note: str | None = None
    reasoning_effort: str | None = None
    openai_reasoning: dict[str, Any] | None = None
    anthropic_thinking: dict[str, Any] | None = None
    anthropic_output_config: dict[str, Any] | None = None
    gemini_thinking_config: dict[str, Any] | None = None
    openrouter_reasoning: dict[str, Any] | None = None


def normalize_reasoning_level(value: str | None) -> ReasoningLevel:
    """Normalize a stored reasoning level, defaulting invalid input to medium."""
    if value in SUPPORTED_REASONING_LEVELS:
        return value
    return "medium"


def resolve_reasoning(model: str, reasoning_level: str | None) -> ReasoningResolution:
    """Resolve a stored reasoning level into provider-specific request settings."""
    stored_level = normalize_reasoning_level(reasoning_level)
    normalized_model = _normalize_model_id(model)

    if normalized_model in {"openai/gpt-5.4", "openai/gpt-5.2"}:
        return _resolve_openai_flagship(normalized_model, stored_level)
    if normalized_model == "openai/gpt-5-mini":
        return _resolve_openai_mini(normalized_model, stored_level)
    if normalized_model in {"anthropic/claude-opus-4-6", "anthropic/claude-sonnet-4-6"}:
        return _resolve_anthropic_46(normalized_model, stored_level)
    if normalized_model in {"gemini/gemini-3.1-pro-preview", "gemini/gemini-3-pro-preview"}:
        return _resolve_gemini_pro(normalized_model, stored_level)
    if normalized_model == "gemini/gemini-3-flash-preview":
        return _resolve_gemini_flash(normalized_model, stored_level)
    if normalized_model.startswith("openrouter/"):
        return _resolve_openrouter(normalized_model, stored_level)

    return ReasoningResolution(
        model=normalized_model,
        stored_level=stored_level,
        effective_level=stored_level,
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


def _resolve_openai_flagship(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    effort_map = {
        "off": "none",
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    if stored_level == "ultra":
        return ReasoningResolution(
            model=model,
            stored_level=stored_level,
            effective_level="ultra",
            reasoning_effort="xhigh",
            openai_reasoning={"effort": "xhigh"},
        )

    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level=stored_level,
        reasoning_effort=effort_map[stored_level],
        openai_reasoning={"effort": effort_map[stored_level]},
    )


def _resolve_openai_mini(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    note = None
    if stored_level != "medium":
        note = "This model uses a fixed medium reasoning level."
    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level="medium",
        note=note,
        reasoning_effort="medium",
        openai_reasoning={"effort": "medium"},
    )


def _resolve_anthropic_46(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    if stored_level == "off":
        return ReasoningResolution(
            model=model,
            stored_level=stored_level,
            effective_level="off",
            anthropic_thinking=None,
            anthropic_output_config=None,
        )

    if stored_level == "ultra" and model != "anthropic/claude-opus-4-6":
        return ReasoningResolution(
            model=model,
            stored_level=stored_level,
            effective_level="high",
            note="This model maps ultra to high.",
            anthropic_thinking={"type": "adaptive"},
            anthropic_output_config={"effort": "high"},
        )

    effort_map = {
        "off": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "ultra": "max",
    }
    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level=stored_level,
        anthropic_thinking=None if stored_level == "off" else {"type": "adaptive"},
        anthropic_output_config={"effort": effort_map[stored_level]},
    )


def _resolve_gemini_pro(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    if stored_level in {"off", "low"}:
        effective_level: ReasoningLevel = "low"
        note = None if stored_level == "low" else "This model maps off to low."
        provider_level = "low"
    else:
        effective_level = "high"
        note = None if stored_level == "high" else f"This model maps {stored_level} to high."
        provider_level = "high"

    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level=effective_level,
        note=note,
        reasoning_effort=provider_level,
        gemini_thinking_config={
            "includeThoughts": True,
            "thinkingLevel": provider_level.upper(),
        },
    )


def _resolve_gemini_flash(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    provider_level_map = {
        "off": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "ultra": "high",
    }
    effective_level: ReasoningLevel = "high" if stored_level == "ultra" else stored_level
    note = "This model maps ultra to high." if stored_level == "ultra" else None

    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level=effective_level,
        note=note,
        reasoning_effort=provider_level_map[stored_level],
        gemini_thinking_config={
            "includeThoughts": True,
            "thinkingLevel": provider_level_map[stored_level].upper(),
        },
    )


def _resolve_openrouter(model: str, stored_level: ReasoningLevel) -> ReasoningResolution:
    if stored_level == "off":
        return ReasoningResolution(
            model=model,
            stored_level=stored_level,
            effective_level="off",
            openrouter_reasoning={"enabled": False},
        )

    effort_map = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "ultra": "xhigh",
    }
    effective_level: ReasoningLevel = stored_level
    note = None
    if stored_level == "ultra" and model in {
        "openrouter/google/gemini-3-pro-preview",
        "openrouter/google/gemini-3.1-pro-preview",
        "openrouter/google/gemini-3-flash-preview",
    }:
        effective_level = "high"
        note = "OpenRouter maps xhigh to high for Gemini 3 models."

    return ReasoningResolution(
        model=model,
        stored_level=stored_level,
        effective_level=effective_level,
        note=note,
        openrouter_reasoning={"enabled": True, "effort": effort_map[stored_level]},
    )
