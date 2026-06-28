"""File system tools: read, write, edit."""

import base64
import difflib
import mimetypes
import re
from pathlib import Path
from typing import Any

from ragnarbot.agent.pathing import resolve_path_in_workspace
from ragnarbot.agent.tools.base import Tool

EDIT_DIFF_MAX_CHARS = 4_000  # only attach a unified diff to edit results below this size

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB (Anthropic API limit for base64 images)

# file_read windowing/caps
MAX_READ_CHARS = 50_000  # hard ceiling on returned text (matches web_fetch default)
DEFAULT_LINE_LIMIT = 2_000  # default number of lines when `limit` is omitted
MAX_LINE_CHARS = 2_000  # truncate any single returned line (minified files)
HARD_FILE_BYTES = 25 * 1024 * 1024  # refuse to load files larger than this


def _resolve_path(path: str, workspace: Path | None = None) -> Path:
    """Resolve a user path, anchoring relative paths to the active workspace."""
    return resolve_path_in_workspace(path, workspace)


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(self, model: str | None = None, workspace: Path | None = None):
        self._model = model
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file at the given path. Returns up to "
            f"{DEFAULT_LINE_LIMIT} lines / {MAX_READ_CHARS} characters per call. "
            "Page through large files with `offset` (1-based start line) and `limit`; "
            "when output is capped, a footer tells you the next offset to use. "
            "Pass `line_numbers=true` to prefix line numbers (do NOT copy those prefixes "
            "into edit_file's old_text). For image files (jpg, png, gif, webp) the content "
            "is returned as a visual image so you can see and describe it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based line number to start reading from. Defaults to 1."
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Maximum number of lines to read from offset. Defaults to "
                        f"{DEFAULT_LINE_LIMIT}. Output is also hard-capped at "
                        f"{MAX_READ_CHARS} characters."
                    )
                },
                "line_numbers": {
                    "type": "boolean",
                    "description": (
                        "Prefix each line with its absolute line number. Default false. "
                        "Do not copy the number prefixes into edit_file's old_text."
                    )
                }
            },
            "required": ["path"]
        }

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
        line_numbers: bool = False,
        **kwargs: Any,
    ) -> str | list[dict[str, Any]]:
        try:
            file_path = _resolve_path(path, self._workspace)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            # Image files → multimodal visual content (offset/limit ignored)
            if file_path.suffix.lower() in IMAGE_EXTENSIONS:
                if self._model:
                    from ragnarbot.config.providers import model_supports_vision
                    if not model_supports_vision(self._model):
                        return (
                            f"Vision is not supported by the current model. "
                            f"Cannot display image: {path}"
                        )
                return self._read_image(file_path, path)

            if file_path.stat().st_size > HARD_FILE_BYTES:
                mb = HARD_FILE_BYTES / (1024 * 1024)
                return (
                    f"Error: File exceeds {mb:.0f} MB read limit: {path}. "
                    f"Use exec with a tool like sed/head to read part of it."
                )

            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return (
                    f"Error: {path} is not valid UTF-8 text (looks binary). "
                    f"Use exec with a tool like xxd/hexdump, or read an image file."
                )

            if content == "":
                return "(file is empty)"

            return self._window(content, offset, limit, line_numbers)
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    @staticmethod
    def _window(
        content: str, offset: int | None, limit: int | None, line_numbers: bool,
    ) -> str:
        """Return a line window of `content`, capped by line count and char budget."""
        lines = content.split("\n")  # split/join on \n → exact round-trip
        total = len(lines)
        start = offset or 1
        if start > total:
            return f"Error: offset {start} is past end of file ({total} lines)."
        eff_limit = limit or DEFAULT_LINE_LIMIT
        end = min(start + eff_limit - 1, total)  # inclusive, 1-based

        emitted: list[str] = []
        chars = 0
        char_capped = False
        last = start - 1
        for n in range(start, end + 1):
            text = lines[n - 1]
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " …(truncated)"
            rendered = f"{n:>6}\t{text}" if line_numbers else text
            # Always emit at least one line so offset can advance.
            if emitted and chars + len(rendered) + 1 > MAX_READ_CHARS:
                char_capped = True
                break
            emitted.append(rendered)
            chars += len(rendered) + 1
            last = n

        body = "\n".join(emitted)
        if start == 1 and last == total and not char_capped:
            return body  # whole file shown → exact, no footer (backward compatible)
        if char_capped:
            return (
                f"{body}\n\n[truncated at {MAX_READ_CHARS}-char cap — showing lines "
                f"{start}-{last} of {total}; continue with offset={last + 1}]"
            )
        return (
            f"{body}\n\n[showing lines {start}-{last} of {total}; "
            f"read more with offset={last + 1}]"
        )

    @staticmethod
    def _read_image(file_path: Path, display_path: str) -> str | list[dict[str, Any]]:
        """Read an image file and return multimodal content blocks."""
        size = file_path.stat().st_size
        if size > MAX_IMAGE_SIZE:
            size_mb = size / (1024 * 1024)
            limit_mb = MAX_IMAGE_SIZE / (1024 * 1024)
            return (
                f"Error: Image file exceeds {limit_mb:.0f} MB size limit "
                f"(actual: {size_mb:.1f} MB). The image cannot be displayed inline. "
                f"Use shell tools to resize or compress it, "
                f"or ask the user to provide a smaller version."
            )

        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "image/jpeg"
        b64 = base64.b64encode(file_path.read_bytes()).decode()
        size_kb = size / 1024

        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_image_path": str(file_path),
                "_mime_type": mime,
            },
            {"type": "text", "text": f"Image: {display_path} ({size_kb:.0f} KB)"},
        ]


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Replace a text block in an existing file. By default `old_text` must match exactly "
            "once — include enough surrounding context to be unique. If an exact match is not "
            "found, a whitespace/indentation-tolerant match is tried automatically and applied "
            "only when it resolves to a single location (otherwise it errors and asks for more "
            "context — it never edits the wrong spot). Pass `replace_all=true` to replace every "
            "occurrence. Always file_read the file first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "The text to find and replace. Include enough surrounding context to "
                        "match exactly one location. If no exact match is found, a "
                        "whitespace-tolerant match is attempted."
                    )
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text, inserted verbatim (include the indentation you want)."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace every exact occurrence instead of requiring a unique match. "
                        "Default false. When false and old_text occurs more than once, the edit "
                        "fails with a count so you can add context."
                    )
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(
        self, path: str, old_text: str, new_text: str,
        replace_all: bool = False, **kwargs: Any,
    ) -> str:
        try:
            file_path = _resolve_path(path, self._workspace)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            if old_text == new_text:
                return "Error: old_text and new_text are identical; nothing to change."

            try:
                # newline="" disables newline translation so CRLF/LF is preserved byte-for-byte.
                with file_path.open("r", encoding="utf-8", newline="") as fh:
                    content = fh.read()
            except UnicodeDecodeError:
                return f"Error: {path} is not valid UTF-8 text (looks binary); cannot edit."

            # Phase 1: exact substring match.
            count = content.count(old_text)
            if count == 1:
                new_content = content.replace(old_text, new_text, 1)
                mode, n = "exact", 1
            elif count > 1 and replace_all:
                new_content = content.replace(old_text, new_text)
                mode, n = "exact", count
            elif count > 1:
                return (
                    f"Error: old_text matches {count} locations. Pass replace_all=true to replace "
                    f"all, or add surrounding context to target exactly one."
                )
            else:
                # Phase 2: whitespace/indentation-tolerant fallback (single span only).
                new_content, err = self._whitespace_tolerant_edit(content, old_text, new_text)
                if err:
                    return err
                mode, n = "whitespace-tolerant", 1

            with file_path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_content)
            return self._success(path, content, new_content, mode, n)
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error editing file: {str(e)}"

    @staticmethod
    def _norm(s: str) -> str:
        """Match key: indentation- and trailing-whitespace-insensitive."""
        return re.sub(r"[ \t]+", " ", s.strip())

    def _whitespace_tolerant_edit(
        self, content: str, old_text: str, new_text: str,
    ) -> tuple[str, str | None]:
        """Return (new_content, None) on a single normalized match, else ('', error)."""
        src_keep = content.splitlines(keepends=True)
        src_plain = [ln.rstrip("\r\n") for ln in src_keep]

        pat = old_text.replace("\r\n", "\n").replace("\r", "\n")
        pat_trailing_nl = pat.endswith("\n")
        pat_lines = pat[:-1].split("\n") if pat_trailing_nl else pat.split("\n")

        pat_keys = [self._norm(x) for x in pat_lines]
        if all(k == "" for k in pat_keys):
            return "", "Error: old_text is only whitespace; provide real content to match."

        src_keys = [self._norm(x) for x in src_plain]
        length = len(pat_keys)
        starts = [
            i for i in range(0, len(src_keys) - length + 1)
            if src_keys[i:i + length] == pat_keys
        ]
        if not starts:
            return "", (
                "Error: old_text not found, even with whitespace-tolerant matching. "
                "file_read the file and copy the exact text."
            )
        if len(starts) > 1:
            return "", (
                f"Error: old_text matches {len(starts)} locations after whitespace-tolerant "
                f"matching. Add more surrounding context so it targets exactly one."
            )

        i = starts[0]
        last = i + length - 1
        char_start = sum(len(src_keep[j]) for j in range(i))
        text_end = char_start + sum(len(src_keep[j]) for j in range(i, last)) + len(src_plain[last])
        term = src_keep[last][len(src_plain[last]):]  # "", "\n", or "\r\n"
        char_end = text_end + (len(term) if pat_trailing_nl else 0)
        new_content = content[:char_start] + new_text + content[char_end:]
        return new_content, None

    @staticmethod
    def _success(path: str, old: str, new: str, mode: str, n: int) -> str:
        header = f"Successfully edited {path} ({n} replacement(s), {mode} match)."
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=path, tofile=path, n=2,
            )
        )
        if diff and len(diff) <= EDIT_DIFF_MAX_CHARS:
            return f"{header}\n{diff}"
        return header


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, self._workspace)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
