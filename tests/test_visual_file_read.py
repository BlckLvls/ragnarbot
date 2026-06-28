"""Tests for visual file support in read_file tool."""

import base64
from pathlib import Path

import pytest

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.tools.filesystem import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE,
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
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

    @pytest.mark.asyncio
    async def test_read_relative_path_resolves_from_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        txt = workspace / "docs" / "readme.txt"
        txt.parent.mkdir(parents=True)
        txt.write_text("hello workspace")

        tool = ReadFileTool(workspace=workspace)
        result = await tool.execute(path="docs/readme.txt")

        assert result == "hello workspace"


class TestFilesystemToolsWorkspaceResolution:
    @pytest.mark.asyncio
    async def test_write_relative_path_resolves_from_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace=workspace)
        result = await tool.execute(path="research/brief.md", content="brief")

        assert "Successfully wrote" in result
        assert (workspace / "research" / "brief.md").read_text() == "brief"

    @pytest.mark.asyncio
    async def test_edit_relative_path_resolves_from_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "notes.txt"
        target.write_text("hello world")

        tool = EditFileTool(workspace=workspace)
        result = await tool.execute(
            path="notes.txt", old_text="hello", new_text="hi",
        )

        assert "Successfully edited" in result
        assert target.read_text() == "hi world"

    @pytest.mark.asyncio
    async def test_list_dir_relative_path_resolves_from_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        docs = workspace / "docs"
        docs.mkdir(parents=True)
        (docs / "a.txt").write_text("a")
        (docs / "b.txt").write_text("b")

        tool = ListDirTool(workspace=workspace)
        result = await tool.execute(path="docs")

        assert "📄 a.txt" in result
        assert "📄 b.txt" in result


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


class TestReadFileWindowing:
    @pytest.mark.asyncio
    async def test_offset_and_limit_slice(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="big.txt", offset=5, limit=5)
        body, _, footer = result.partition("\n\n[")
        assert body.splitlines() == [f"line{i}" for i in range(5, 10)]
        assert "showing lines 5-9 of 50" in footer
        assert "offset=10" in footer

    @pytest.mark.asyncio
    async def test_limit_without_offset_starts_at_one(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="big.txt", limit=3)
        assert result.split("\n\n[")[0].splitlines() == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_offset_past_eof(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("a\nb\nc", encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="small.txt", offset=99)
        assert "past end of file" in result

    @pytest.mark.asyncio
    async def test_char_cap_and_footer(self, tmp_path):
        f = tmp_path / "huge.txt"
        # 1000 lines of 200 chars ~ 200k chars, well over the 50k cap
        f.write_text("\n".join("x" * 200 for _ in range(1000)), encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="huge.txt")
        assert len(result) < 60_000  # capped
        assert "truncated at 50000-char cap" in result
        assert "offset=" in result

    @pytest.mark.asyncio
    async def test_line_numbers_on_and_off(self, tmp_path):
        f = tmp_path / "n.txt"
        f.write_text("alpha\nbeta", encoding="utf-8")
        plain = await ReadFileTool(workspace=tmp_path).execute(path="n.txt")
        assert plain == "alpha\nbeta"
        numbered = await ReadFileTool(workspace=tmp_path).execute(path="n.txt", line_numbers=True)
        assert "1\talpha" in numbered
        assert "2\tbeta" in numbered

    @pytest.mark.asyncio
    async def test_binary_file_graceful(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\x00\x01\x02\xff\xfe valid? no")
        result = await ReadFileTool(workspace=tmp_path).execute(path="blob.bin")
        assert "not valid UTF-8" in result or "binary" in result

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="empty.txt")
        assert result == "(file is empty)"

    @pytest.mark.asyncio
    async def test_small_file_no_footer(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text("one\ntwo\nthree", encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="s.txt")
        assert result == "one\ntwo\nthree"
        assert "[showing lines" not in result

    @pytest.mark.asyncio
    async def test_trailing_newline_roundtrip(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("one\ntwo\n", encoding="utf-8")
        result = await ReadFileTool(workspace=tmp_path).execute(path="t.txt")
        assert result == "one\ntwo\n"


class TestEditFileRobust:
    @pytest.mark.asyncio
    async def test_exact_single(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("hello world", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="world", new_text="there"
        )
        assert "Successfully edited" in result
        assert f.read_text() == "hello there"

    @pytest.mark.asyncio
    async def test_replace_all(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\na\na\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="a", new_text="X", replace_all=True
        )
        assert "3 replacement(s)" in result
        assert f.read_text() == "X\nX\nX\n"

    @pytest.mark.asyncio
    async def test_ambiguous_without_replace_all_errors(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\na\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="a", new_text="X"
        )
        assert result.startswith("Error: old_text matches 2 locations")
        assert f.read_text() == "a\na\n"  # unchanged

    @pytest.mark.asyncio
    async def test_whitespace_tolerant_single(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo():\n        return 1\n", encoding="utf-8")  # 8-space indent
        result = await EditFileTool(workspace=tmp_path).execute(
            path="code.py",
            old_text="def foo():\n    return 1",  # 4-space indent
            new_text="def foo():\n    return 2",
        )
        assert "whitespace-tolerant" in result
        assert f.read_text() == "def foo():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_whitespace_tolerant_ambiguous_errors(self, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("if a:\n    do()\nif a:\n  do()\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.py", old_text="if a:\n do()", new_text="if a:\n done()"
        )
        assert "matches 2 locations after whitespace-tolerant" in result
        assert f.read_text() == "if a:\n    do()\nif a:\n  do()\n"  # unchanged

    @pytest.mark.asyncio
    async def test_whitespace_tolerant_not_found(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="gamma\ndelta", new_text="x"
        )
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_crlf_preserved_on_untouched_lines(self, tmp_path):
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"alpha\r\n    target\r\nomega\r\n")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="crlf.txt",
            old_text="        target",  # 8-space indent forces the tolerant path
            new_text="    target_done",
        )
        assert "whitespace-tolerant" in result
        assert f.read_bytes() == b"alpha\r\n    target_done\r\nomega\r\n"

    @pytest.mark.asyncio
    async def test_identical_text_errors(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("same", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="same", new_text="same"
        )
        assert "identical" in result
        assert f.read_text() == "same"

    @pytest.mark.asyncio
    async def test_whitespace_only_old_text_errors(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="   \n  ", new_text="x"
        )
        assert "only whitespace" in result

    @pytest.mark.asyncio
    async def test_deletion(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("keep\nremove me\nkeep\n", encoding="utf-8")
        result = await EditFileTool(workspace=tmp_path).execute(
            path="f.txt", old_text="remove me\n", new_text=""
        )
        assert "Successfully edited" in result
        assert f.read_text() == "keep\nkeep\n"
