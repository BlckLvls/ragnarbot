"""Tests for OAuth token refresh logic."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from ragnarbot.auth.credentials import Credentials
from ragnarbot.auth.oauth import ensure_valid_token, refresh_access_token


@pytest.mark.asyncio
async def test_ensure_valid_token_returns_none_when_no_access_token():
    """No access token returns None."""
    creds = Credentials()
    creds.providers.anthropic.api_key = "sk-test"
    result = await ensure_valid_token(creds)
    assert result is None


@pytest.mark.asyncio
async def test_ensure_valid_token_returns_token_when_not_expired():
    """Valid, non-expired token is returned as-is."""
    creds = Credentials()
    creds.providers.anthropic.oauth.access_token = "sk-ant-oat-valid"
    creds.providers.anthropic.oauth.expires_at = int(time.time()) + 3600
    result = await ensure_valid_token(creds)
    assert result == "sk-ant-oat-valid"


@pytest.mark.asyncio
async def test_ensure_valid_token_returns_token_when_no_expiry():
    """Token with expires_at=0 is assumed valid."""
    creds = Credentials()
    creds.providers.anthropic.oauth.access_token = "sk-ant-oat-noexpiry"
    creds.providers.anthropic.oauth.expires_at = 0
    result = await ensure_valid_token(creds)
    assert result == "sk-ant-oat-noexpiry"


@pytest.mark.asyncio
async def test_ensure_valid_token_refreshes_expired_token():
    """Expired token triggers refresh."""
    creds = Credentials()
    creds.providers.anthropic.oauth.access_token = "old-token"
    creds.providers.anthropic.oauth.refresh_token = "refresh-tok"
    creds.providers.anthropic.oauth.expires_at = int(time.time()) - 100

    mock_result = {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
    }

    with (
        patch("ragnarbot.auth.oauth.refresh_access_token", new_callable=AsyncMock) as mock_refresh,
        patch("ragnarbot.auth.credentials.save_credentials") as mock_save,
    ):
        mock_refresh.return_value = mock_result
        result = await ensure_valid_token(creds)

    assert result == "new-token"
    assert creds.providers.anthropic.oauth.access_token == "new-token"
    assert creds.providers.anthropic.oauth.refresh_token == "new-refresh"
    mock_save.assert_called_once_with(creds)


@pytest.mark.asyncio
async def test_ensure_valid_token_returns_stale_on_refresh_failure():
    """If refresh fails, return existing (potentially stale) token."""
    creds = Credentials()
    creds.providers.anthropic.oauth.access_token = "stale-token"
    creds.providers.anthropic.oauth.refresh_token = "bad-refresh"
    creds.providers.anthropic.oauth.expires_at = int(time.time()) - 100

    with patch(
        "ragnarbot.auth.oauth.refresh_access_token",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    ):
        result = await ensure_valid_token(creds)

    assert result == "stale-token"


@pytest.mark.asyncio
async def test_ensure_valid_token_no_refresh_token():
    """Expired token without refresh token returns stale."""
    creds = Credentials()
    creds.providers.anthropic.oauth.access_token = "stale-token"
    creds.providers.anthropic.oauth.refresh_token = ""
    creds.providers.anthropic.oauth.expires_at = int(time.time()) - 100

    result = await ensure_valid_token(creds)
    assert result == "stale-token"
