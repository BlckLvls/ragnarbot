"""Search tools: grep (content search) and glob (filename discovery).

These exist so the agent never has to shell out to `exec` + `grep`/`find` for
routine navigation. Running through these tools (instead of the shell) avoids
shell-quoting pitfalls, the exec output cap, and the workspace safety-guard, and
returns clean, capped, workspace-relative results.
"""

import asyncio
import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any

from ragnarbot.agent.pathing import resolve_path_in_workspace
from ragnarbot.agent.tools.base import Tool
from ragnarbot.agent.tools.ripgrep import ensure_ripgrep

MAX_LINE_CHARS = 500  # truncate any single rendered line to this many chars
NULL_PROBE_BYTES = 8192  # bytes sniffed to detect binary files
PY_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
}


def _parse_duration(text: str) -> int | None:
    """Parse '30m' / '24h' / '7d' / '90s' / '120' (seconds) into seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(NULL_PROBE_BYTES)
    except OSError:
        return True


def _matches_glob(rel: str, name: str, glob: str | None) -> bool:
    if not glob:
        return True
    if "/" in glob or "**" in glob:
        return fnmatch.fnmatch(rel, glob)
    return fnmatch.fnmatch(name, glob)


class GrepTool(Tool):
    """Search file contents for a regular expression."""

    def __init__(
        self,
        workspace: Path | None = None,
        backend: str = "auto",
        max_matches: int = 200,
        max_output_chars: int = 20000,
        timeout: int = 30,
        auto_install: bool = True,
    ):
        self._workspace = workspace
        self.backend = backend
        self.max_matches = max_matches
        self.max_output_chars = max_output_chars
        self.timeout = timeout
        self.auto_install = auto_install

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents with a regular expression. Prefer this over `exec` with "
            "shell grep/rg — it handles regex escaping, stays scoped to the workspace, returns "
            "clean path:line:text output, and is never blocked by the shell safety guard. "
            "Filter files with `glob` (e.g. '*.py'), pick verbosity with `output_mode`, and "
            "add `context_lines` for surrounding context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Regular expression to search for (e.g. 'def \\w+_handler').",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search. Defaults to the workspace root.",
                },
                "glob": {
                    "type": "string",
                    "description": "Filter files by name, e.g. '*.md' or '*.py'.",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive match. Default false.",
                },
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Lines of context before and after each match. Default 0.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": (
                        "content = path:line:text (default); files_with_matches = matching file "
                        "paths; count = path:count per file."
                    ),
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Cap on matches (content) or files (other modes).",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        output_mode: str = "content",
        max_matches: int | None = None,
        **kwargs: Any,
    ) -> str:
        cap = max_matches or self.max_matches
        root = resolve_path_in_workspace(path or ".", self._workspace)
        if not root.exists():
            return f"Error: path not found: {path or '.'}"

        if self.backend == "python":
            use_rg = None
        else:
            use_rg = await ensure_ripgrep(self._data_root(), allow_download=self.auto_install)
            if self.backend == "ripgrep" and not use_rg:
                return (
                    "Error: ripgrep (rg) is not available and could not be installed "
                    "automatically. Install it (e.g. 'brew install ripgrep'), or set "
                    "tools.search.backend to 'auto' or 'python'."
                )

        try:
            if use_rg:
                lines, truncated, err = await self._run_rg(
                    use_rg, pattern, root, glob, case_insensitive,
                    context_lines, output_mode, cap,
                )
                if err:
                    return err
            else:
                lines, truncated = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._run_python, pattern, root, glob, case_insensitive,
                        context_lines, output_mode, cap,
                    ),
                    timeout=self.timeout,
                )
        except asyncio.TimeoutError:
            return f"Error: grep timed out after {self.timeout}s. Narrow the path or pattern."
        except re.error as exc:
            return f"Error: invalid regular expression: {exc}"

        if not lines:
            return "No matches found."
        return self._format(lines, truncated, output_mode, cap)

    @staticmethod
    def _data_root() -> Path | None:
        """Profile data root for caching an auto-installed rg binary."""
        try:
            from ragnarbot.instance import get_instance
            return get_instance().data_root
        except Exception:
            return None

    def _format(self, lines: list[str], truncated: bool, output_mode: str, cap: int) -> str:
        body = "\n".join(lines)
        notes = []
        if len(body) > self.max_output_chars:
            body = body[: self.max_output_chars]
            notes.append(f"... (output truncated at {self.max_output_chars} chars)")
        if truncated:
            unit = "matches" if output_mode == "content" else "files"
            notes.append(f"... (stopped at {cap} {unit}; refine the search to see more)")
        if notes:
            body = f"{body}\n" + "\n".join(notes)
        return body

    @staticmethod
    def _search_base(root: Path) -> tuple[Path, str]:
        """Return (cwd, target) so output paths are relative to the search base."""
        if root.is_file():
            return root.parent, root.name
        return root, "."

    async def _run_rg(
        self, rg, pattern, root, glob, case_insensitive,
        context_lines, output_mode, cap,
    ) -> tuple[list[str], bool, str | None]:
        cwd, target = self._search_base(root)
        args = [rg, "--line-number", "--no-heading", "--color", "never", "--sort", "path"]
        if case_insensitive:
            args.append("-i")
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        elif context_lines:
            args += ["-C", str(context_lines)]
        if glob:
            args += ["-g", glob]
        args += ["-e", pattern, "--", target]

        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        lines: list[str] = []
        truncated = False
        try:
            while True:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("./"):  # rg prints './path' when searching '.'
                    line = line[2:]
                if len(line) > MAX_LINE_CHARS:
                    line = line[:MAX_LINE_CHARS] + " …(truncated)"
                lines.append(line)
                if len(lines) >= cap:
                    truncated = True
                    break
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if truncated:
            proc.kill()
        await proc.wait()
        if not truncated and proc.returncode == 2:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
            first = stderr.splitlines()[0] if stderr else "ripgrep error"
            return [], False, f"Error: {first}"
        return lines, truncated, None

    def _run_python(
        self, pattern, root, glob, case_insensitive,
        context_lines, output_mode, cap,
    ) -> tuple[list[str], bool]:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        base = root.parent if root.is_file() else root

        rendered: list[str] = []
        units = 0
        truncated = False
        for fpath in self._iter_files(root, glob):
            if _is_binary(fpath):
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = self._rel(fpath, base)
            file_lines = text.split("\n")
            match_idx = [i for i, ln in enumerate(file_lines) if regex.search(ln)]
            if not match_idx:
                continue

            if output_mode == "files_with_matches":
                rendered.append(rel)
                units += 1
            elif output_mode == "count":
                rendered.append(f"{rel}:{len(match_idx)}")
                units += 1
            else:  # content
                emitted, n = self._render_content(
                    rel, file_lines, match_idx, context_lines, cap - units
                )
                rendered.extend(emitted)
                units += n
                if len(match_idx) > n:  # this file alone overflowed the cap
                    truncated = True
                    break
            if units >= cap:
                truncated = True
                break
        return rendered, truncated

    def _iter_files(self, root: Path, glob: str | None):
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in PY_SKIP_DIRS)
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = self._rel(full, root)
                if _matches_glob(rel, fn, glob):
                    yield full

    @staticmethod
    def _rel(path: Path, base: Path) -> str:
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    @staticmethod
    def _render_content(
        rel: str, file_lines: list[str], match_idx: list[int], context_lines: int, remaining: int,
    ) -> tuple[list[str], int]:
        if remaining <= 0:
            return [], 0
        keep = match_idx[:remaining]
        matchset = set(keep)
        show: set[int] = set()
        for i in keep:
            for j in range(max(0, i - context_lines), min(len(file_lines), i + context_lines + 1)):
                show.add(j)
        out: list[str] = []
        prev: int | None = None
        for j in sorted(show):
            if prev is not None and j != prev + 1:
                out.append("--")
            sep = ":" if j in matchset else "-"
            text = file_lines[j]
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " …(truncated)"
            out.append(f"{rel}{sep}{j + 1}{sep}{text}")
            prev = j
        return out, len(keep)


class GlobTool(Tool):
    """Find files by name pattern, most-recently-modified first."""

    def __init__(
        self,
        workspace: Path | None = None,
        max_results: int = 200,
        max_output_chars: int = 20000,
        timeout: int = 30,
    ):
        self._workspace = workspace
        self.max_results = max_results
        self.max_output_chars = max_output_chars
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files by name pattern (e.g. '**/*.md', 'src/**/*.py'), sorted by modification "
            "time (most recent first). Cleaner and faster than `exec` with `find`, scoped to the "
            "workspace. Use `modified_within` (e.g. '24h') to find recently changed files. "
            "Returns workspace-relative paths, one per line."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Glob pattern, e.g. '**/*.py' (recursive) or 'docs/*.md'.",
                },
                "path": {
                    "type": "string",
                    "description": "Base directory to search. Defaults to the workspace root.",
                },
                "sort": {
                    "type": "string",
                    "enum": ["mtime", "name"],
                    "description": "'mtime' = most recently modified first (default), 'name' = alphabetical.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max number of files to return.",
                },
                "modified_within": {
                    "type": "string",
                    "description": "Only files modified within this window, e.g. '30m', '24h', '7d', or seconds.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str | None = None,
        sort: str = "mtime",
        limit: int | None = None,
        modified_within: str | None = None,
        **kwargs: Any,
    ) -> str:
        cap = limit or self.max_results
        base = resolve_path_in_workspace(path or ".", self._workspace)
        if not base.exists():
            return f"Error: path not found: {path or '.'}"
        if not base.is_dir():
            return f"Error: path is not a directory: {path or '.'}"

        cutoff: float | None = None
        if modified_within:
            secs = _parse_duration(modified_within)
            if secs is None:
                return f"Error: invalid modified_within '{modified_within}'. Use e.g. '30m', '24h', '7d'."
            cutoff = time.time() - secs

        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(self._collect, base, pattern, cutoff),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return f"Error: glob timed out after {self.timeout}s. Narrow the pattern or path."
        except (NotImplementedError, ValueError) as exc:
            return f"Error: invalid glob pattern '{pattern}': {exc}"

        if not hits:
            within = f" modified within {modified_within}" if modified_within else ""
            return f"No files matching '{pattern}'{within} under {path or 'the workspace'}."

        if sort == "name":
            hits.sort(key=lambda h: h[0].lower())
        else:
            hits.sort(key=lambda h: h[1], reverse=True)

        truncated = len(hits) > cap
        shown = hits[:cap]
        body = "\n".join(rel for rel, _ in shown)
        notes = []
        if len(body) > self.max_output_chars:
            body = body[: self.max_output_chars]
            notes.append(f"... (output truncated at {self.max_output_chars} chars)")
        if truncated:
            notes.append(
                f"... showing {cap} of {len(hits)} files "
                f"({len(hits) - cap} omitted; narrow the pattern or raise limit)"
            )
        if notes:
            body = f"{body}\n" + "\n".join(notes)
        return body

    @staticmethod
    def _collect(base: Path, pattern: str, cutoff: float | None) -> list[tuple[str, float]]:
        hits: list[tuple[str, float]] = []
        for p in base.glob(pattern):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p
            if any(part in PY_SKIP_DIRS for part in rel.parts):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            hits.append((str(rel), st.st_mtime))
        return hits
