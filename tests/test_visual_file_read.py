"""Tests for visual file support in read_file tool."""

import base64
from pathlib import Path

import pytest

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.tools.filesystem import IMAGE_EXTENSIONS, MAX_IMAGE_SIZE, ReadFileTool
from ragnarbot.session.manager import Session

# -- Helpers ------------------------------------------------------------------

def _make_png(path: Path, size: int = 100) -> None:
    """Write a minimal valid PNG file."""
    # Minimal 1x1 white PNG (67 bytes)
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQAB"
        "Nl7BcQAAAABJRU5ErkJggg=="
    )
    if size > len(png_bytes):
        # Pad with trailing zeros (still valid enough for our purposes)
        png_bytes = png_bytes + b"\x00" * (size - len(png_bytes))
    path.write_bytes(png_bytes)


# -- ReadFileTool tests -------------------------------------------------------

class TestReadFileToolImages:
    @pytest.mark.asyncio
    async def test_image_returns_multimodal_blocks(self, tmp_path):
        img = tmp_path / "photo.png"
        _make_png(img)

        tool = ReadFileTool()
        result = await tool.execute(path=str(img))

        assert isinstance(result, list)
        assert len(result) == 2

        # Image block
        img_block = result[0]
        assert img_block["type"] == "image_url"
        assert img_block["image_url"]["url"].startswith("data:image/png;base64,")
        assert img_block["_image_path"] == str(img.resolve())
        assert img_block["_mime_type"] == "image/png"

        # Text block
        text_block = result[1]
        assert text_block["type"] == "text"
        assert "photo.png" in text_block["text"]

    @pytest.mark.asyncio
    async def test_text_file_returns_string(self, tmp_path):
        txt = tmp_path / "readme.txt"
        txt.write_text("hello world")

        tool = ReadFileTool()
        result = await tool.execute(path=str(txt))

        assert isinstance(result, str)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_all_image_extensions_detected(self, tmp_path):
        tool = ReadFileTool()
        for ext in IMAGE_EXTENSIONS:
            img = tmp_path / f"test{ext}"
            _make_png(img)
            result = await tool.execute(path=str(img))
            assert isinstance(result, list), f"Extension {ext} not detected as image"

    @pytest.mark.asyncio
    async def test_svg_treated_as_text(self, tmp_path):
        svg = tmp_path / "icon.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

        tool = ReadFileTool()
        result = await tool.execute(path=str(svg))

        assert isinstance(result, str)
        assert "<svg" in result

    @pytest.mark.asyncio
    async def test_large_image_rejected(self, tmp_path):
        img = tmp_path / "huge.jpg"
        # Write a file just over the size limit
        img.write_bytes(b"\xff\xd8\xff" + b"\x00" * (MAX_IMAGE_SIZE + 1))

        tool = ReadFileTool()
        result = await tool.execute(path=str(img))

        assert isinstance(result, str)
        assert "exceeds" in result.lower()
        assert "size limit" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_file_error(self):
        tool = ReadFileTool()
        result = await tool.execute(path="/nonexistent/photo.png")

        assert isinstance(result, str)
        assert "not found" in result.lower()


# -- Session persistence tests ------------------------------------------------

class TestSessionImagePersistence:
    def test_add_message_strips_base64(self):
        session = Session(key="test", user_key="test:1")
        content = [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "_image_path": "/tmp/photo.png",
                "_mime_type": "image/png",
            },
            {"type": "text", "text": "Image: /tmp/photo.png (5 KB)"},
        ]

        session.add_message("tool", content, tool_call_id="tc_1", name="file_read")

        msg = session.messages[-1]
        # Content should be plain text, not the list
        assert isinstance(msg["content"], str)
        assert "photo.png" in msg["content"]
        assert "base64" not in msg["content"]
        # image_refs should be stored
        assert "image_refs" in msg
        assert len(msg["image_refs"]) == 1
        assert msg["image_refs"][0]["path"] == "/tmp/photo.png"
        assert msg["image_refs"][0]["mime"] == "image/png"

    def test_add_message_string_content_unchanged(self):
        session = Session(key="test", user_key="test:1")
        session.add_message("tool", "some text result", tool_call_id="tc_1", name="exec")

        msg = session.messages[-1]
        assert msg["content"] == "some text result"
        assert "image_refs" not in msg

    def test_get_history_resolves_image_refs(self, tmp_path):
        img = tmp_path / "photo.png"
        _make_png(img)

        session = Session(key="test", user_key="test:1")
        # Manually add a message with image_refs (simulating loaded from disk)
        session.messages.append({
            "role": "user",
            "content": "read this image",
            "metadata": {"timestamp": "2026-01-01T00:00:00"},
        })
        session.messages.append({
            "role": "tool",
            "content": f"Image: {img} (0 KB)",
            "metadata": {"timestamp": "2026-01-01T00:00:01"},
            "tool_call_id": "tc_1",
            "name": "file_read",
            "image_refs": [{"path": str(img), "mime": "image/png"}],
        })

        history = session.get_history()
        tool_msg = [m for m in history if m["role"] == "tool"][0]

        # Should be re-encoded as multimodal content
        assert isinstance(tool_msg["content"], list)
        assert any(b.get("type") == "image_url" for b in tool_msg["content"])
        assert any(b.get("type") == "text" for b in tool_msg["content"])

    def test_get_history_graceful_when_file_missing(self):
        session = Session(key="test", user_key="test:1")
        session.messages.append({
            "role": "user",
            "content": "read this image",
            "metadata": {"timestamp": "2026-01-01T00:00:00"},
        })
        session.messages.append({
            "role": "tool",
            "content": "Image: /nonexistent/gone.png (5 KB)",
            "metadata": {"timestamp": "2026-01-01T00:00:01"},
            "tool_call_id": "tc_1",
            "name": "file_read",
            "image_refs": [{"path": "/nonexistent/gone.png", "mime": "image/png"}],
        })

        history = session.get_history()
        tool_msg = [m for m in history if m["role"] == "tool"][0]

        # Should gracefully degrade to text-only
        assert isinstance(tool_msg["content"], str)
        assert "gone.png" in tool_msg["content"]


# -- Cache flush tests --------------------------------------------------------

class TestCacheFlushImages:
    def test_flush_downgrades_multimodal_to_text(self):
        messages = [
            {"role": "tool", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "text", "text": "Image: photo.png (5 KB)"},
            ]},
        ]

        count = CacheManager._flush_tool_results(messages, "soft")

        assert count == 1
        assert isinstance(messages[0]["content"], str)
        assert "photo.png" in messages[0]["content"]

    def test_flush_leaves_string_content_alone_if_short(self):
        messages = [
            {"role": "tool", "content": "short result"},
        ]

        count = CacheManager._flush_tool_results(messages, "soft")

        assert count == 0
        assert messages[0]["content"] == "short result"


# -- Anthropic provider conversion tests --------------------------------------

class TestAnthropicMultimodalToolResult:
    def test_list_content_converted(self):
        from ragnarbot.providers.anthropic_provider import AnthropicProvider

        messages = [
            {"role": "user", "content": "read the image"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc_1", "type": "function",
                 "function": {"name": "file_read", "arguments": '{"path": "/tmp/photo.png"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc_1", "name": "file_read", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "text", "text": "Image: /tmp/photo.png (5 KB)"},
            ]},
        ]

        _, anthropic_msgs = AnthropicProvider._convert_messages(messages)

        # Find the tool_result block
        tool_result_msg = None
        for msg in anthropic_msgs:
            if msg["role"] == "user" and isinstance(msg["content"], list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_msg = block
                        break

        assert tool_result_msg is not None
        # Content should be converted (list of Anthropic blocks, not raw OpenAI format)
        content = tool_result_msg["content"]
        assert isinstance(content, list)
        # Should contain an image block and a text block
        types = [b.get("type") for b in content]
        assert "image" in types
        assert "text" in types


# -- LiteLLM sanitize tests ---------------------------------------------------

class TestLiteLLMSanitize:
    def test_strips_underscore_keys(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "tool", "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                    "_image_path": "/tmp/photo.png",
                    "_mime_type": "image/png",
                },
                {"type": "text", "text": "Image: photo.png"},
            ]},
        ]

        result = LiteLLMProvider._sanitize_messages(messages)

        img_block = result[0]["content"][0]
        assert "_image_path" not in img_block
        assert "_mime_type" not in img_block
        assert img_block["type"] == "image_url"

    def test_leaves_string_content_alone(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [{"role": "tool", "content": "hello"}]
        result = LiteLLMProvider._sanitize_messages(messages)

        assert result[0]["content"] == "hello"
