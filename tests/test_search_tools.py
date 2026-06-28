"""Tests for the grep and glob search tools."""

import asyncio
import os
import shutil
import time
from unittest.mock import MagicMock

import pytest

from ragnarbot.agent.tools.search import GlobTool, GrepTool


def _make_corpus(root):
    """Build a small workspace corpus and return the root path."""
    (root / "notes.md").write_text("alpha\nBeta\ngamma\n", encoding="utf-8")
    (root / "many.md").write_text("alpha\nalpha\nalpha\n", encoding="utf-8")
    (root / "dash.md").write_text("foo-xbar\n", encoding="utf-8")
    (root / "longline.md").write_text("x" * 600 + "alpha\n", encoding="utf-8")
    (root / "bin.dat").write_bytes(b"\x00\x01alpha\x00data")
    sub = root / "sub"
    sub.mkdir()
    (sub / "code.py").write_text("def handler():\n    return 1\n# alpha here\n", encoding="utf-8")
    (sub / "data.txt").write_text("nothing relevant\n", encoding="utf-8")
    return root


# --------------------------- grep ---------------------------

@pytest.mark.asyncio
async def test_grep_content_mode(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="notes.md")
    assert result == "notes.md:1:alpha"


@pytest.mark.asyncio
async def test_grep_finds_across_files(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha here")
    assert "sub/code.py:3:# alpha here" in result


@pytest.mark.asyncio
async def test_grep_smart_case(tmp_path):
    _make_corpus(tmp_path)  # notes.md has "Beta" on line 2
    tool = GrepTool(workspace=tmp_path, backend="python")
    # lowercase pattern -> smart-case insensitive -> matches "Beta"
    assert await tool.execute(pattern="beta", path="notes.md") == "notes.md:2:Beta"
    # uppercase letter in pattern -> case-sensitive -> "BETA" doesn't match "Beta"
    assert await tool.execute(pattern="BETA", path="notes.md") == "No matches found."
    # explicit override: force insensitive
    assert await tool.execute(
        pattern="BETA", path="notes.md", case_insensitive=True
    ) == "notes.md:2:Beta"
    # explicit override: force sensitive
    assert await tool.execute(
        pattern="beta", path="notes.md", case_insensitive=False
    ) == "No matches found."


@pytest.mark.asyncio
async def test_grep_glob_filter(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", glob="*.md", output_mode="files_with_matches")
    assert "notes.md" in result
    assert "sub/code.py" not in result  # .py excluded by *.md filter


@pytest.mark.asyncio
async def test_grep_context_lines(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="Beta", path="notes.md", context_lines=1)
    lines = result.splitlines()
    assert "notes.md-1-alpha" in lines
    assert "notes.md:2:Beta" in lines
    assert "notes.md-3-gamma" in lines


@pytest.mark.asyncio
async def test_grep_files_with_matches(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", output_mode="files_with_matches")
    assert "notes.md" in result
    assert "sub/code.py" in result
    assert ":" not in result.split("\n")[0]  # paths only, no line:text


@pytest.mark.asyncio
async def test_grep_count_mode(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="many.md", output_mode="count")
    assert result == "many.md:3"


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    # A scoped (non-workspace) search returns the bare message, no hint.
    result = await tool.execute(pattern="zzznotpresent", path="sub")
    assert result == "No matches found."


@pytest.mark.asyncio
async def test_grep_cap_and_footer(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="many.md", max_matches=2)
    lines = result.splitlines()
    assert lines[0] == "many.md:1:alpha"
    assert lines[1] == "many.md:2:alpha"
    assert "stopped at 2 matches" in result


@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="(")
    assert result.startswith("Error: invalid regular expression")


@pytest.mark.asyncio
async def test_grep_workspace_anchoring(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="sub")
    assert "code.py:3:# alpha here" in result
    assert "sub/code.py" not in result  # paths are relative to the searched dir


@pytest.mark.asyncio
async def test_grep_skips_binary(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", output_mode="files_with_matches")
    assert "bin.dat" not in result


@pytest.mark.asyncio
async def test_grep_per_line_cap(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="longline.md")
    assert "…(truncated)" in result


@pytest.mark.asyncio
async def test_grep_pattern_with_leading_dash(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="-x", path="dash.md")
    assert result == "dash.md:1:foo-xbar"


@pytest.mark.asyncio
async def test_grep_path_not_found(tmp_path):
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="alpha", path="nope")
    assert result.startswith("Error: path not found")


@pytest.mark.asyncio
async def test_grep_ripgrep_missing_explicit(tmp_path, monkeypatch):
    _make_corpus(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    # auto_install=False + sandboxed data_root keeps this deterministic (no download,
    # no dependency on a binary cached in the real profile).
    tool = GrepTool(
        workspace=tmp_path, backend="ripgrep", auto_install=False, data_root=tmp_path
    )
    result = await tool.execute(pattern="alpha")
    assert "ripgrep" in result and "could not be installed" in result


def test_grep_rg_argv_disables_gitignore():
    args = GrepTool._rg_argv("rg", "foo", ".", None, False, 0, "content")
    assert "--no-ignore" in args  # do not honor .gitignore
    assert "--hidden" in args  # search dotfiles too (matches Python fallback)
    assert "!node_modules" in args  # but still prune known junk dirs
    assert "!.git" in args


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_grep_ripgrep_ignores_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("findme please\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, backend="ripgrep", data_root=tmp_path)
    result = await tool.execute(pattern="findme", output_mode="files_with_matches")
    assert "secret.txt" in result  # gitignored file is still searched


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_grep_ripgrep_python_parity(tmp_path):
    corpus = tmp_path / "clean"
    corpus.mkdir()
    (corpus / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (corpus / "b.txt").write_text("gamma\nalpha\n", encoding="utf-8")
    rg_tool = GrepTool(workspace=tmp_path, backend="ripgrep", data_root=tmp_path)
    py_tool = GrepTool(workspace=tmp_path, backend="python", data_root=tmp_path)
    rg = await rg_tool.execute(pattern="alpha", path="clean", output_mode="files_with_matches")
    py = await py_tool.execute(pattern="alpha", path="clean", output_mode="files_with_matches")
    assert set(rg.splitlines()) == set(py.splitlines()) == {"a.txt", "b.txt"}


# --------------------------- glob ---------------------------

@pytest.mark.asyncio
async def test_glob_recursive_md(tmp_path):
    _make_corpus(tmp_path)
    (tmp_path / "sub" / "deep.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.md")
    paths = set(result.splitlines())
    assert "notes.md" in paths
    assert "sub/deep.md" in paths


@pytest.mark.asyncio
async def test_glob_sort_mtime(tmp_path):
    old = tmp_path / "old.md"
    new = tmp_path / "new.md"
    old.write_text("o", encoding="utf-8")
    new.write_text("n", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 1000, now - 1000))
    os.utime(new, (now, now))
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.md", sort="mtime")
    lines = result.splitlines()
    assert lines.index("new.md") < lines.index("old.md")


@pytest.mark.asyncio
async def test_glob_sort_name(tmp_path):
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.md", sort="name")
    assert result.splitlines() == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_glob_limit_and_footer(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.md", limit=2)
    body = [ln for ln in result.splitlines() if not ln.startswith("...")]
    assert len(body) == 2
    assert "showing 2 of 5 files" in result


@pytest.mark.asyncio
async def test_glob_modified_within(tmp_path):
    recent = tmp_path / "recent.md"
    stale = tmp_path / "stale.md"
    recent.write_text("r", encoding="utf-8")
    stale.write_text("s", encoding="utf-8")
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))  # 2 hours ago
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.md", modified_within="1h")
    assert "recent.md" in result
    assert "stale.md" not in result


@pytest.mark.asyncio
async def test_glob_no_match(tmp_path):
    _make_corpus(tmp_path)
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.zzz")
    assert result.startswith("No files matching")


@pytest.mark.asyncio
async def test_glob_relative_paths(tmp_path):
    _make_corpus(tmp_path)
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.md")
    assert str(tmp_path) not in result


@pytest.mark.asyncio
async def test_glob_only_files(tmp_path):
    _make_corpus(tmp_path)
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*")
    assert "sub" not in result.splitlines()  # the directory itself is excluded


@pytest.mark.asyncio
async def test_glob_subdir_scope(tmp_path):
    _make_corpus(tmp_path)
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.py", path="sub")
    assert result.splitlines() == ["code.py"]


@pytest.mark.asyncio
async def test_glob_skips_ignored_dirs(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "junk.md").write_text("x", encoding="utf-8")
    (tmp_path / "real.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.md")
    assert "real.md" in result
    assert "junk.md" not in result


@pytest.mark.asyncio
async def test_glob_invalid_modified_within(tmp_path):
    _make_corpus(tmp_path)
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.md", modified_within="soon")
    assert result.startswith("Error: invalid modified_within")


# --------------------------- long-line handling (no readline 64KB crash) ---------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not available")
async def test_rg_reader_handles_giant_line(tmp_path):
    """A line longer than asyncio's 64KB readline limit must not crash."""
    big = tmp_path / "big.txt"
    big.write_text("x" * 200_000 + "\n" + "short\n", encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        "cat", str(big),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    tool = GrepTool(workspace=tmp_path)
    lines, truncated = await tool._read_rg_output(proc, cap=100)
    await proc.wait()
    assert len(lines) == 2
    assert lines[0].endswith("…(truncated)")  # giant line truncated, not crashed
    assert lines[1] == "short"


@pytest.mark.asyncio
async def test_grep_python_handles_giant_line(tmp_path):
    (tmp_path / "giant.md").write_text(
        "x" * 200_000 + " needle " + "y" * 50_000 + "\n", encoding="utf-8"
    )
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="needle", path="giant.md")
    assert "giant.md:1:" in result
    assert "…(truncated)" in result  # matched line truncated, no crash


# --------------------------- smart-case matching ---------------------------

def test_ci_glob_pattern_expands_letters():
    from ragnarbot.agent.tools.search import _ci_glob_pattern
    assert _ci_glob_pattern("*soul*") == "*[sS][oO][uU][lL]*"
    assert _ci_glob_pattern("**/*.md") == "**/*.[mM][dD]"  # ** and . preserved
    # letters inside an existing [...] class are left alone
    assert _ci_glob_pattern("[abc]x") == "[abc][xX]"


@pytest.mark.asyncio
async def test_glob_smart_case_lowercase_matches_uppercase_file(tmp_path):
    (tmp_path / "SOUL.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*soul*")
    assert "SOUL.md" in result  # the original footgun, now fixed


@pytest.mark.asyncio
async def test_glob_uppercase_pattern_is_case_sensitive(tmp_path):
    (tmp_path / "Readme.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    # lowercase -> smart insensitive -> matches
    assert "Readme.md" in await tool.execute(pattern="*readme*")
    # uppercase letter -> case-sensitive -> 'README' != 'Readme'
    assert "Readme.md" not in await tool.execute(pattern="*README*")


@pytest.mark.asyncio
async def test_glob_explicit_case_sensitive(tmp_path):
    (tmp_path / "SOUL.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*soul*", case_insensitive=False)
    assert result.startswith("No files matching")


@pytest.mark.asyncio
async def test_glob_smart_case_preserves_recursion(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "Config.yaml").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*config*")
    assert "a/b/Config.yaml" in result  # ** recursion still works under smart-case


# --------------------------- searching outside the workspace ---------------------------

@pytest.mark.asyncio
async def test_glob_absolute_path_escapes_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "found.md").write_text("x", encoding="utf-8")
    tool = GlobTool(workspace=ws)
    result = await tool.execute(pattern="**/*.md", path=str(outside))
    assert "found.md" in result


@pytest.mark.asyncio
async def test_grep_absolute_path_escapes_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "note.txt").write_text("needle here\n", encoding="utf-8")
    tool = GrepTool(workspace=ws, backend="python")
    result = await tool.execute(pattern="needle", path=str(outside))
    assert "note.txt:1:needle here" in result


@pytest.mark.asyncio
async def test_glob_empty_hints_absolute_path(tmp_path):
    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.nonexistent")
    assert "absolute path" in result


@pytest.mark.asyncio
async def test_grep_empty_hints_absolute_path(tmp_path):
    _make_corpus(tmp_path)
    tool = GrepTool(workspace=tmp_path, backend="python")
    result = await tool.execute(pattern="zzznotpresent")
    assert "absolute path" in result


# --------------------------- registry wiring ---------------------------

def test_subagent_registry_includes_search(tmp_path):
    """General-purpose subagents get grep/glob via SAFE_TOOL_NAMES."""
    from ragnarbot.agent.agents_loader import AgentsLoader
    from ragnarbot.agent.subagent import SubagentManager

    provider = MagicMock()
    provider.get_default_model.return_value = "test/model"
    loader = AgentsLoader(tmp_path / "ws", builtin_agents_dir=tmp_path / "empty")
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path / "ws",
        bus=MagicMock(),
        agents_loader=loader,
        model="test/model",
    )
    reg, _ = mgr._build_agent_tool_registry(None, "cli", "direct")
    assert "grep" in reg.tool_names
    assert "glob" in reg.tool_names
