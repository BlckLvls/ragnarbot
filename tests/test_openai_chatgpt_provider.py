"""Tests for the OpenAI ChatGPT OAuth provider."""

from unittest.mock import patch

from ragnarbot.providers.openai_chatgpt_provider import OpenAIChatGPTProvider


def test_openai_chatgpt_provider_defaults_to_gpt_5_4():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    assert provider.default_model == "gpt-5.4"


def test_openai_chatgpt_provider_builds_priority_service_tier_for_lightning():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    body = provider._build_request(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="gpt-5.4",
        reasoning_level="medium",
        lightning_mode=True,
    )

    assert body["service_tier"] == "priority"
