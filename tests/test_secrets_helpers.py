"""Tests for secrets_helpers module."""

from unittest.mock import patch

from ragnarbot.agent.tools.secrets_helpers import (
    check_config_dependency,
    secrets_get,
    secrets_list,
    secrets_schema,
    secrets_set,
)
from ragnarbot.auth.credentials import (
    Credentials,
    ProviderCredentials,
    ProvidersCredentials,
    ServiceCredential,
    ServicesCredentials,
)

LOAD_CREDS = "ragnarbot.agent.tools.secrets_helpers.load_credentials"


def _creds_with_anthropic_key() -> Credentials:
    return Credentials(
        providers=ProvidersCredentials(
            anthropic=ProviderCredentials(api_key="sk-ant-test123"),
        ),
    )


def _creds_with_anthropic_oauth() -> Credentials:
    return Credentials(
        providers=ProvidersCredentials(
            anthropic=ProviderCredentials(oauth_key="oauth-test123"),
        ),
    )


def _creds_with_gemini_key() -> Credentials:
    return Credentials(
        providers=ProvidersCredentials(
            gemini=ProviderCredentials(api_key="gemini-key-123"),
        ),
    )


def _empty_creds() -> Credentials:
    return Credentials()


# --- check_config_dependency ---


def test_check_dependency_blocks_missing_credential():
    with patch(LOAD_CREDS, return_value=_empty_creds()):
        result = check_config_dependency("agents.defaults.model", "gemini/gemini-2.5-pro")
    assert result is not None
    assert "Blocked" in result
    assert "gemini" in result


def test_check_dependency_passes_with_credential():
    with patch(LOAD_CREDS, return_value=_creds_with_anthropic_key()):
        result = check_config_dependency("agents.defaults.model", "anthropic/claude-3")
    assert result is None


def test_check_dependency_oauth_satisfies():
    with patch(LOAD_CREDS, return_value=_creds_with_anthropic_oauth()):
        result = check_config_dependency("agents.defaults.model", "anthropic/claude-3")
    assert result is None


def test_check_dependency_irrelevant_path():
    with patch(LOAD_CREDS, return_value=_empty_creds()):
        result = check_config_dependency("agents.defaults.debounce_seconds", "1.0")
    assert result is None


def test_check_dependency_brave_blocks_without_key():
    with patch(LOAD_CREDS, return_value=_empty_creds()):
        result = check_config_dependency("tools.web.search.engine", "brave")
    assert result is not None
    assert "brave" in result.lower()


def test_check_dependency_brave_passes_with_key():
    creds = Credentials(
        services=ServicesCredentials(
            brave_search=ServiceCredential(api_key="brave-key-123"),
        ),
    )
    with patch(LOAD_CREDS, return_value=creds):
        result = check_config_dependency("tools.web.search.engine", "brave")
    assert result is None


# --- secrets_get ---


def test_secrets_get_structured():
    creds = _creds_with_anthropic_key()
    result = secrets_get(creds, "providers.anthropic.api_key")
    assert "sk-ant-test123" in result
    assert '"reload": "warm"' in result


def test_secrets_get_extra():
    creds = Credentials(extra={"github_token": "ghp_abc123"})
    result = secrets_get(creds, "extra.github_token")
    assert "ghp_abc123" in result


def test_secrets_get_extra_missing():
    creds = _empty_creds()
    result = secrets_get(creds, "extra.nonexistent")
    assert "Error" in result


def test_secrets_get_invalid_path():
    creds = _empty_creds()
    result = secrets_get(creds, "nonexistent.path.here")
    assert "Error" in result


# --- secrets_set ---


def test_secrets_set_structured():
    creds = _empty_creds()
    creds, result = secrets_set(creds, "providers.anthropic.api_key", "new-key-123")
    assert '"status": "saved"' in result
    assert creds.providers.anthropic.api_key == "new-key-123"


def test_secrets_set_extra():
    creds = _empty_creds()
    creds, result = secrets_set(creds, "extra.github_token", "ghp_xxx")
    assert '"status": "saved"' in result
    assert creds.extra["github_token"] == "ghp_xxx"


# --- secrets_schema ---


def test_secrets_schema_masking():
    creds = _creds_with_anthropic_key()
    result = secrets_schema(creds)
    assert "secrets.providers.anthropic.api_key [set \u2713]" in result
    assert "secrets.providers.anthropic.oauth_key [not set \u2717]" in result


def test_secrets_schema_shows_extra():
    creds = Credentials(extra={"my_key": "val"})
    result = secrets_schema(creds)
    assert "secrets.extra.my_key [set \u2713]" in result


def test_secrets_schema_filter():
    creds = _creds_with_anthropic_key()
    result = secrets_schema(creds, "secrets.providers.anthropic")
    assert "secrets.providers.anthropic.api_key" in result
    assert "secrets.services" not in result


# --- secrets_list ---


def test_secrets_list_masking():
    creds = _creds_with_anthropic_key()
    result = secrets_list(creds)
    assert "secrets.providers.anthropic.api_key = ****" in result
    assert "secrets.providers.anthropic.oauth_key = [not set]" in result


def test_secrets_list_extra():
    creds = Credentials(extra={"github_token": "ghp_abc"})
    result = secrets_list(creds)
    assert "secrets.extra.github_token = ****" in result
