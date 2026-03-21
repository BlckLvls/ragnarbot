"""Shared path resolution helpers for agent runtime."""

from pathlib import Path


def resolve_workspace_root(workspace: Path | str | None = None) -> Path:
    """Resolve the active workspace root or fall back to the current directory."""
    if workspace is None:
        return Path.cwd().resolve()
    return Path(workspace).expanduser().resolve()


def resolve_path_in_workspace(path: str | Path, workspace: Path | str | None = None) -> Path:
    """Resolve a path, anchoring relative inputs under the active workspace."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() and workspace is not None:
        resolved = resolve_workspace_root(workspace) / resolved
    return resolved.resolve()


def resolve_working_dir(
    working_dir: str | None = None,
    workspace: Path | str | None = None,
) -> Path:
    """Resolve an execution working directory against the active workspace."""
    if working_dir:
        return resolve_path_in_workspace(working_dir, workspace)
    return resolve_workspace_root(workspace)
