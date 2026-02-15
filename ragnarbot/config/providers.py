"""Provider and model registry for ragnarbot."""

from ragnarbot.config.schema import OAUTH_SUPPORTED_PROVIDERS

PROVIDERS = [
    {
        "id": "anthropic",
        "name": "Anthropic",
        "description": "Claude models (Opus, Sonnet, Haiku)",
        "api_key_url": "https://console.anthropic.com/keys",
        "models": [
            {
                "id": "anthropic/claude-opus-4-6",
                "name": "Claude Opus 4.6",
                "description": "Most intelligent — agents & coding",
            },
            {
                "id": "anthropic/claude-sonnet-4-5",
                "name": "Claude Sonnet 4.5",
                "description": "Best speed/intelligence balance",
            },
            {
                "id": "anthropic/claude-haiku-4-5",
                "name": "Claude Haiku 4.5",
                "description": "Fastest — near-frontier intelligence",
            },
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "description": "GPT models (GPT-5.2, GPT-5 Mini)",
        "api_key_url": "https://platform.openai.com/api-keys",
        "models": [
            {
                "id": "openai/gpt-5.2",
                "name": "GPT-5.2",
                "description": "Most capable — reasoning & coding",
            },
            {
                "id": "openai/gpt-5-mini",
                "name": "GPT-5 Mini",
                "description": "Fast & affordable",
            },
        ],
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "description": "Google models (Gemini 3 Pro, Flash)",
        "api_key_url": "https://aistudio.google.dev/apikey",
        "models": [
            {
                "id": "gemini/gemini-3-pro-preview",
                "name": "Gemini 3 Pro",
                "description": "Advanced reasoning & multimodal",
            },
            {
                "id": "gemini/gemini-3-flash-preview",
                "name": "Gemini 3 Flash",
                "description": "Fast — near-Pro intelligence",
            },
        ],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "description": "Multi-provider gateway (Minimax, Kimi, GLM, and more)",
        "api_key_url": "https://openrouter.ai/keys",
        "models": [
            {
                "id": "openrouter/minimax/minimax-m2.5",
                "name": "Minimax M2.5",
                "description": "Fast reasoning model",
                "vision": False,
                "providers": ["minimax", "novita"],
            },
            {
                "id": "openrouter/z-ai/glm-5",
                "name": "GLM-5",
                "description": "Strong multilingual reasoning",
                "vision": False,
                "providers": [
                    "siliconflow", "atlas-cloud", "gmicloud", "friendli",
                    "together", "z-ai", "fireworks", "novita",
                ],
            },
            {
                "id": "openrouter/moonshotai/kimi-k2.5",
                "name": "Kimi K2.5",
                "description": "Long-context reasoning & coding",
            },
            {
                "id": "openrouter/anthropic/claude-opus-4.6",
                "name": "Claude Opus 4.6",
                "description": "Most intelligent — via OpenRouter",
            },
            {
                "id": "openrouter/google/gemini-3-flash-preview",
                "name": "Gemini 3 Flash",
                "description": "Fast — via OpenRouter",
            },
            {
                "id": "openrouter/openai/gpt-5.2",
                "name": "GPT-5.2",
                "description": "Most capable — via OpenRouter",
            },
            {
                "id": "openrouter/google/gemini-3-pro-preview",
                "name": "Gemini 3 Pro",
                "description": "Advanced reasoning — via OpenRouter",
            },
        ],
    },
]


def get_provider(provider_id: str) -> dict | None:
    """Get a provider by ID."""
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return None


def get_models(provider_id: str) -> list[dict]:
    """Get models for a provider."""
    provider = get_provider(provider_id)
    if provider:
        return provider["models"]
    return []


def supports_oauth(provider_id: str) -> bool:
    """Check if a provider supports OAuth authentication."""
    return provider_id in OAUTH_SUPPORTED_PROVIDERS


def get_model_info(model_id: str) -> dict | None:
    """Get model dict by its full ID (e.g. 'openrouter/minimax/minimax-m2.5')."""
    for p in PROVIDERS:
        for m in p["models"]:
            if m["id"] == model_id:
                return m
    return None


def model_supports_vision(model_id: str) -> bool:
    """Check if a model supports vision. Unknown models default to True."""
    info = get_model_info(model_id)
    if info is None:
        return True
    return info.get("vision", True)
