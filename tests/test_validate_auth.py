"""Tests for startup auth validation (_validate_auth) including fallback."""

from unittest.mock import patch

import pytest

from ragnarbot.auth.credentials import (
    Credentials,
    ProviderCredentials,
    ProvidersCredentials,
)
from ragnarbot.cli.commands import _validate_auth
from ragnarbot.config.schema import Config


@pytest.fixture
def anthropic_oauth_creds():
    return Credentials(
        providers=ProvidersCredentials(
            anthropic=ProviderCredentials(oauth_key="ant-oauth-token"),
        ),
    )


@pytest.fixture
def mixed_creds():
    """Anthropic OAuth + Gemini API key — the Issue #70 setup."""
    return Credentials(
        providers=ProvidersCredentials(
            anthropic=ProviderCredentials(oauth_key="ant-oauth-token"),
            gemini=ProviderCredentials(api_key="gemini-api-key"),
        ),
    )


def test_primary_only_valid(anthropic_oauth_creds):
    config = Config()
    config.agents.defaults.auth_method = "oauth"
    assert _validate_auth(config, anthropic_oauth_creds) is None


def test_primary_and_fallback_different_providers_valid(mixed_creds):
    """Issue #70: primary=Anthropic/OAuth, fallback=Gemini/api_key — should pass."""
    config = Config()
    config.agents.defaults.auth_method = "oauth"
    config.agents.fallback.model = "gemini/gemini-3-pro-preview"
    config.agents.fallback.auth_method = "api_key"
    assert _validate_auth(config, mixed_creds) is None


def test_fallback_no_model_skips_validation(anthropic_oauth_creds):
    """No fallback model configured — should not trigger fallback validation."""
    config = Config()
    config.agents.defaults.auth_method = "oauth"
    # fallback.model defaults to None
    assert _validate_auth(config, anthropic_oauth_creds) is None


def test_fallback_wrong_auth_method_fails(anthropic_oauth_creds):
    """Fallback model set but auth_method doesn't match credentials."""
    config = Config()
    config.agents.defaults.auth_method = "oauth"
    config.agents.fallback.model = "gemini/gemini-3-pro-preview"
    config.agents.fallback.auth_method = "api_key"
    # anthropic_oauth_creds has no Gemini api_key
    error = _validate_auth(config, anthropic_oauth_creds)
    assert error is not None
    assert "Fallback model" in error
    assert "gemini" in error.lower()


def test_fallback_oauth_no_token_fails():
    """Fallback uses OAuth but no OAuth configured for that provider."""
    creds = Credentials(
        providers=ProvidersCredentials(
            anthropic=ProviderCredentials(api_key="ant-key"),
            gemini=ProviderCredentials(api_key="gemini-key"),
        ),
    )
    config = Config()
    config.agents.defaults.auth_method = "api_key"
    config.agents.fallback.model = "gemini/gemini-3-pro-preview"
    config.agents.fallback.auth_method = "oauth"
    with patch("ragnarbot.auth.gemini_oauth.is_authenticated", return_value=False):
        error = _validate_auth(config, creds)
    assert error is not None
    assert "Fallback model" in error


def test_primary_invalid_auth_method():
    config = Config()
    config.agents.defaults.auth_method = "bearer"
    error = _validate_auth(config, Credentials())
    assert error is not None
    assert "Unknown auth method" in error
