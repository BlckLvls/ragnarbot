"""Tests for the OpenAI ChatGPT OAuth provider."""

from unittest.mock import patch

from ragnarbot.providers.openai_chatgpt_provider import OpenAIChatGPTProvider


def test_openai_chatgpt_provider_defaults_to_gpt_5_4():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    assert provider.default_model == "gpt-5.4"
