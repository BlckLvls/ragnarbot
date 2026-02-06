"""Tests for OAuth token helper."""

from ragnarbot.auth.credentials import Credentials
from ragnarbot.auth.oauth import get_oauth_token


def test_get_oauth_token_returns_none_when_empty():
    """No oauth_key returns None."""
    creds = Credentials()
    assert get_oauth_token(creds) is None


def test_get_oauth_token_returns_token():
    """Returns oauth_key when set."""
    creds = Credentials()
    creds.providers.anthropic.oauth_key = "sk-ant-oat-valid"
    assert get_oauth_token(creds) == "sk-ant-oat-valid"
