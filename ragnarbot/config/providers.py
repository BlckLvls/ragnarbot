"""Provider and model registry for ragnarbot."""

from dataclasses import dataclass

from ragnarbot.config.schema import CUSTOM_PROVIDER_PREFIX, OAUTH_SUPPORTED_PROVIDERS

PROVIDERS = [
    {
        "id": "anthropic",
        "name": "Anthropic",
        "description": "Claude models (Opus, Sonnet, Haiku)",
        "api_key_url": "https://console.anthropic.com/keys",
        "models": [
            {
                "id": "anthropic/claude-opus-4-8",
                "name": "Claude Opus 4.8",
                "description": "Most intelligent — agents & coding",
            },
            {
                "id": "anthropic/claude-opus-4-7",
                "name": "Claude Opus 4.7",
                "description": "Previous flagship — agents & coding",
            },
            {
                "id": "anthropic/claude-sonnet-4-6",
                "name": "Claude Sonnet 4.6",
                "description": "Best speed/intelligence balance",
            },
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "description": "GPT models (GPT-5.6 Sol, Terra, Luna, and earlier)",
        "api_key_url": "https://platform.openai.com/api-keys",
        "models": [
            {
                "id": "openai/gpt-5.6-sol",
                "name": "GPT-5.6 Sol",
                "description": "Flagship — most capable GPT-5.6 model",
            },
            {
                "id": "openai/gpt-5.6-terra",
                "name": "GPT-5.6 Terra",
                "description": "Balanced — strong lower-cost GPT-5.6 option",
            },
            {
                "id": "openai/gpt-5.6-luna",
                "name": "GPT-5.6 Luna",
                "description": "Fastest & most affordable GPT-5.6 model",
                # The public API supports Luna, but ChatGPT OAuth's raw
                # Responses transport currently returns `Model not found`.
                "oauth": False,
            },
            {
                "id": "openai/gpt-5.5",
                "name": "GPT-5.5",
                "description": "Previous flagship — strong reasoning & coding",
            },
            {
                "id": "openai/gpt-5.4",
                "name": "GPT-5.4",
                "description": "Earlier flagship — strong reasoning & coding",
            },
            {
                "id": "openai/gpt-5.4-mini",
                "name": "GPT-5.4 Mini",
                "description": "Fast & affordable",
            },
        ],
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "description": "Google models (Gemini 3.1 Pro, 3 Pro, Flash)",
        "api_key_url": "https://aistudio.google.dev/apikey",
        "models": [
            {
                "id": "gemini/gemini-3.1-pro-preview",
                "name": "Gemini 3.1 Pro",
                "description": "Best reasoning & coding — latest flagship",
            },
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
                "id": "openrouter/qwen/qwen3.5-plus-02-15",
                "name": "Qwen 3.5 Plus",
                "description": "Strong multilingual reasoning & coding",
            },
            {
                "id": "openrouter/anthropic/claude-opus-4.8",
                "name": "Claude Opus 4.8",
                "description": "Most intelligent — via OpenRouter",
            },
            {
                "id": "openrouter/anthropic/claude-opus-4.7",
                "name": "Claude Opus 4.7",
                "description": "Previous flagship — via OpenRouter",
            },
            {
                "id": "openrouter/anthropic/claude-sonnet-4.6",
                "name": "Claude Sonnet 4.6",
                "description": "Best speed/intelligence balance — via OpenRouter",
            },
            {
                "id": "openrouter/google/gemini-3-flash-preview",
                "name": "Gemini 3 Flash",
                "description": "Fast — via OpenRouter",
            },
            {
                "id": "openrouter/google/gemini-3.1-pro-preview",
                "name": "Gemini 3.1 Pro",
                "description": "Best reasoning & coding — via OpenRouter",
            },
            {
                "id": "openrouter/openai/gpt-5.5",
                "name": "GPT-5.5",
                "description": "Latest flagship — via OpenRouter",
            },
            {
                "id": "openrouter/openai/gpt-5.4",
                "name": "GPT-5.4",
                "description": "Previous flagship — via OpenRouter",
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


def get_models(provider_id: str, auth_method: str | None = None) -> list[dict]:
    """Get models for a provider, optionally filtered by auth method."""
    provider = get_provider(provider_id)
    if provider:
        models = provider["models"]
        if auth_method == "oauth":
            return [model for model in models if model.get("oauth", True)]
        return models
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
    if is_custom_model(model_id):
        resolved = resolve_custom_model(model_id)
        # Local models rarely accept images — default to False unless declared.
        return bool(resolved and resolved.vision)
    info = get_model_info(model_id)
    if info is None:
        return True
    return info.get("vision", True)


# ── custom OpenAI-compatible servers ─────────────────────────────

@dataclass(frozen=True)
class ResolvedCustomModel:
    """A custom model id resolved against the configured servers."""

    provider_id: str
    provider_name: str
    base_url: str
    model_id: str  # bare model id as the server knows it
    vision: bool = False
    max_tokens: int | None = None

    @property
    def api_key(self) -> str | None:
        """API key for this server, if one is stored in credentials."""
        from ragnarbot.auth.credentials import load_credentials

        key = load_credentials().extra.get(custom_provider_secret_name(self.provider_id), "")
        return key or None


def custom_provider_secret_name(provider_id: str) -> str:
    """Credentials `extra` key holding a custom server's API key."""
    return f"custom_{provider_id}_api_key"


def custom_model_id(provider_id: str, model_id: str) -> str:
    """Build the full model identifier for a custom server model."""
    return f"{CUSTOM_PROVIDER_PREFIX}/{provider_id}/{model_id}"


def is_custom_model(model_id: str) -> bool:
    """Check if a model id points at a configured custom server."""
    return model_id.startswith(f"{CUSTOM_PROVIDER_PREFIX}/")


def split_custom_model(model_id: str) -> tuple[str, str] | None:
    """Split 'custom/<server>/<model>' into (server_id, bare_model_id)."""
    if not is_custom_model(model_id):
        return None
    rest = model_id.split("/", 1)[1]
    if "/" not in rest:
        return None
    provider_id, bare = rest.split("/", 1)
    if not provider_id or not bare:
        return None
    return provider_id, bare


def resolve_custom_model(model_id: str, config=None) -> ResolvedCustomModel | None:
    """Resolve a 'custom/<server>/<model>' id against configured servers.

    Returns None when the id is not a custom model or the server/model
    is not configured.
    """
    parts = split_custom_model(model_id)
    if parts is None:
        return None
    provider_id, bare = parts

    if config is None:
        from ragnarbot.config.loader import load_config
        config = load_config()

    for server in config.custom_providers:
        if server.id != provider_id:
            continue
        entry = next((m for m in server.models if m.id == bare), None)
        return ResolvedCustomModel(
            provider_id=server.id,
            provider_name=server.name or server.id,
            base_url=server.base_url,
            model_id=bare,
            vision=bool(entry.vision) if entry else False,
            max_tokens=entry.max_tokens if entry else None,
        )
    return None
