"""Tests for model/auth compatibility validation."""

from unittest.mock import patch

import pytest

from ragnarbot.auth.credentials import Credentials
from ragnarbot.config.validation import validate_model_auth


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-5.6", "gpt-5.6-sol"),
        ("openai/gpt-5.6-luna", "Sol or Terra"),
    ],
)
def test_openai_oauth_rejects_models_unsupported_by_chatgpt_transport(model, expected):
    with patch("ragnarbot.auth.openai_oauth.is_authenticated", return_value=True):
        error = validate_model_auth(model, "oauth", Credentials())

    assert error is not None
    assert expected in error


def test_openai_oauth_accepts_sol():
    with patch("ragnarbot.auth.openai_oauth.is_authenticated", return_value=True):
        assert validate_model_auth(
            "openai/gpt-5.6-sol", "oauth", Credentials(),
        ) is None


def test_openai_api_key_still_accepts_luna():
    credentials = Credentials()
    credentials.providers.openai.api_key = "sk-test"

    assert validate_model_auth(
        "openai/gpt-5.6-luna", "api_key", credentials,
    ) is None
