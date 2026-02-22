"""Shared auth validation for model + auth_method pairs."""

from __future__ import annotations


def validate_model_auth(model: str, auth_method: str, creds=None) -> str | None:
    """Validate that credentials exist for a model + auth_method combination.

    Args:
        model: Model identifier (e.g. "gemini/gemini-3-pro-preview").
        auth_method: Either "api_key" or "oauth".
        creds: Pre-loaded Credentials object. Loaded from disk if None.

    Returns:
        Error message string if validation fails, None if OK.
    """
    from ragnarbot.config.schema import OAUTH_SUPPORTED_PROVIDERS

    if creds is None:
        from ragnarbot.auth.credentials import load_credentials
        creds = load_credentials()

    provider_name = model.split("/")[0] if "/" in model else "anthropic"
    provider_creds = getattr(creds.providers, provider_name, None)

    if auth_method == "oauth":
        if provider_name not in OAUTH_SUPPORTED_PROVIDERS:
            return (
                f"OAuth is not supported for provider '{provider_name}'. "
                f"Supported: {', '.join(sorted(OAUTH_SUPPORTED_PROVIDERS))}"
            )
        if provider_name == "gemini":
            from ragnarbot.auth.gemini_oauth import is_authenticated
            if not is_authenticated():
                return "Gemini OAuth not configured. Run: ragnarbot oauth gemini"
        elif provider_name == "openai":
            from ragnarbot.auth.openai_oauth import is_authenticated
            if not is_authenticated():
                return "OpenAI OAuth not configured. Run: ragnarbot oauth openai"
        elif not provider_creds or not provider_creds.oauth_key:
            return f"No OAuth token for '{provider_name}'"
    else:
        if not provider_creds or not provider_creds.api_key:
            return (
                f"No API key for '{provider_name}'. "
                f"Set via: config set secrets.providers.{provider_name}.api_key <key>"
            )

    return None
