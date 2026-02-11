"""Tests for WebSearchTool with multiple search engines."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.agent.tools.web import WebSearchTool
from ragnarbot.config.schema import WebSearchConfig


class TestWebSearchToolBrave:
    @pytest.mark.asyncio
    async def test_brave_search_returns_formatted_results(self):
        """Brave engine returns formatted results from API."""
        tool = WebSearchTool(engine="brave", api_key="test-key")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Python 3.13 Release",
                        "url": "https://python.org/3.13",
                        "description": "New features in Python 3.13",
                    },
                    {
                        "title": "Python Changelog",
                        "url": "https://docs.python.org/changelog",
                        "description": "Full changelog",
                    },
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("ragnarbot.agent.tools.web.httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            result = await tool.execute(query="python 3.13")

        assert "Results for: python 3.13" in result
        assert "1. Python 3.13 Release" in result
        assert "https://python.org/3.13" in result
        assert "2. Python Changelog" in result

    @pytest.mark.asyncio
    async def test_brave_search_no_api_key(self):
        """Brave engine without API key returns error."""
        tool = WebSearchTool(engine="brave", api_key=None)
        # Clear env var fallback
        with patch.dict("os.environ", {}, clear=True):
            tool.api_key = ""
            result = await tool.execute(query="test")
        assert "Error" in result
        assert "BRAVE_API_KEY" in result

    @pytest.mark.asyncio
    async def test_brave_search_no_results(self):
        """Brave engine with empty results."""
        tool = WebSearchTool(engine="brave", api_key="test-key")

        mock_response = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("ragnarbot.agent.tools.web.httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            result = await tool.execute(query="nonexistent")

        assert "No results for: nonexistent" in result


class TestWebSearchToolDuckDuckGo:
    @pytest.mark.asyncio
    async def test_ddg_search_returns_formatted_results(self):
        """DuckDuckGo engine returns formatted results."""
        tool = WebSearchTool(engine="duckduckgo")

        mock_results = [
            {
                "title": "Python Homepage",
                "href": "https://python.org",
                "body": "The official Python website",
            },
            {
                "title": "Python Tutorial",
                "href": "https://docs.python.org/tutorial",
                "body": "Start learning Python",
            },
        ]

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = mock_results
            mock_ddgs_cls.return_value = mock_ddgs

            result = await tool.execute(query="python")

        assert "Results for: python" in result
        assert "1. Python Homepage" in result
        assert "https://python.org" in result
        assert "2. Python Tutorial" in result
        assert "Start learning Python" in result

    @pytest.mark.asyncio
    async def test_ddg_search_no_api_key_needed(self):
        """DuckDuckGo works without any API key."""
        tool = WebSearchTool(engine="duckduckgo")
        assert tool.api_key == ""  # No env var set

        mock_results = [
            {"title": "Test", "href": "https://example.com", "body": "Test result"},
        ]

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = mock_results
            mock_ddgs_cls.return_value = mock_ddgs

            result = await tool.execute(query="test")

        assert "Results for: test" in result
        assert "1. Test" in result

    @pytest.mark.asyncio
    async def test_ddg_search_no_results(self):
        """DuckDuckGo with empty results."""
        tool = WebSearchTool(engine="duckduckgo")

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = []
            mock_ddgs_cls.return_value = mock_ddgs

            result = await tool.execute(query="nonexistent")

        assert "No results for: nonexistent" in result

    @pytest.mark.asyncio
    async def test_ddg_search_handles_exception(self):
        """DuckDuckGo handles exceptions gracefully."""
        tool = WebSearchTool(engine="duckduckgo")

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs_cls.side_effect = Exception("Rate limited")

            result = await tool.execute(query="test")

        assert "Error:" in result
        assert "Rate limited" in result

    @pytest.mark.asyncio
    async def test_ddg_search_respects_count(self):
        """DuckDuckGo passes max_results correctly."""
        tool = WebSearchTool(engine="duckduckgo")

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = []
            mock_ddgs_cls.return_value = mock_ddgs

            await tool.execute(query="test", count=3)

        mock_ddgs.text.assert_called_once_with("test", max_results=3)


class TestWebSearchToolCommon:
    @pytest.mark.asyncio
    async def test_count_clamped_to_range(self):
        """Count parameter is clamped to 1-10."""
        tool = WebSearchTool(engine="duckduckgo")

        with patch("ddgs.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = []
            mock_ddgs_cls.return_value = mock_ddgs

            # count=0 is falsy, falls back to max_results default (5)
            await tool.execute(query="test", count=0)
            mock_ddgs.text.assert_called_with("test", max_results=5)

            # count=20 should be clamped to 10
            await tool.execute(query="test", count=20)
            mock_ddgs.text.assert_called_with("test", max_results=10)

    def test_default_engine_is_brave(self):
        """Default engine should be brave for backward compatibility."""
        tool = WebSearchTool()
        assert tool.engine == "brave"

    def test_tool_schema(self):
        """Tool schema should be valid OpenAI function format."""
        tool = WebSearchTool()
        schema = tool.to_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "web_search"
        assert "query" in schema["function"]["parameters"]["properties"]


class TestWebSearchConfig:
    def test_default_engine(self):
        """Default engine is brave."""
        config = WebSearchConfig()
        assert config.engine == "brave"

    def test_brave_engine(self):
        """Brave engine is valid."""
        config = WebSearchConfig(engine="brave")
        assert config.engine == "brave"

    def test_duckduckgo_engine(self):
        """DuckDuckGo engine is valid."""
        config = WebSearchConfig(engine="duckduckgo")
        assert config.engine == "duckduckgo"

    def test_invalid_engine(self):
        """Invalid engine should raise validation error."""
        with pytest.raises(Exception):
            WebSearchConfig(engine="google")
