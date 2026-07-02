## Summary

- Add first-class `grep` (ripgrep-backed, with a pure-Python fallback) and `glob` (pathlib) search tools. The bot previously had no content/file search and had to shotgun `exec` + shell-grep, which broke on escaping, head-only truncation, and the workspace safety guard.
- When ripgrep is missing, `grep` auto-downloads the official BurntSushi binary into the profile data root on first use (mirroring the Chromium auto-install) and caches it; if the download fails it falls back to the Python backend.
- Search behaves predictably for an assistant: defaults to the workspace but accepts an absolute `path` to search anywhere on the machine; matching is smart-case; `.gitignore` is not honored so nothing is silently skipped; output lines over 64KB no longer crash the ripgrep backend.
- Harden `file_read` with `offset`/`limit` paging, a hard char/line cap plus a continuation footer, optional line numbers, and graceful binary/oversized handling — a bare read of a large file no longer blows the context window.
- Make `edit_file` robust: add `replace_all`, turn the ambiguous-match soft no-op into a hard error, and add an automatic whitespace/indentation-tolerant fallback that only applies to a single span (never edits the wrong spot); CRLF/LF preserved byte-for-byte.
- Add a `tools.search` config block (backend, caps, timeout, auto-install), register the tools across all registries (interactive, isolated cron/hook, and sub-agent), and document them plus a "File & Text Navigation Hierarchy" in the builtin prompt files.
