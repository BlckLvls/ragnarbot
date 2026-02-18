"""Smart config schema migration for ragnarbot."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from ragnarbot.auth.credentials import (
    Credentials,
    get_credentials_path,
    save_credentials,
)
from ragnarbot.config.loader import (
    convert_keys,
    get_config_path,
    save_config,
)
from ragnarbot.config.schema import Config

# Paths that contain sensitive data (mask values in display)
SENSITIVE_PATHS = {"providers", "api_key", "oauth_key", "bot_token", "api_key_url"}


@dataclass
class MigrationResult:
    """Structured result from a schema migration."""

    data: dict = field(default_factory=dict)
    added: dict[str, Any] = field(default_factory=dict)
    auto_removed: dict[str, Any] = field(default_factory=dict)
    needs_confirm: dict[str, Any] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.auto_removed or self.needs_confirm)


_USER_OWNED_PATHS = {"extra"}


def _deep_diff(
    existing: dict, default: dict, path: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk two dicts, return (added, removed) as flat path->value maps."""
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}

    all_keys = set(existing.keys()) | set(default.keys())
    for key in all_keys:
        current_path = f"{path}.{key}" if path else key
        in_existing = key in existing
        in_default = key in default

        # Skip user-owned freeform dicts — never diff their contents
        if key in _USER_OWNED_PATHS:
            if in_default and not in_existing:
                added[current_path] = default[key]
            continue

        if in_default and not in_existing:
            added[current_path] = default[key]
        elif in_existing and not in_default:
            removed[current_path] = existing[key]
        elif in_existing and in_default:
            if isinstance(existing[key], dict) and isinstance(default[key], dict):
                sub_added, sub_removed = _deep_diff(existing[key], default[key], current_path)
                added.update(sub_added)
                removed.update(sub_removed)

    return added, removed


def _is_sensitive(path: str) -> bool:
    """Check if a config path contains sensitive data."""
    parts = path.split(".")
    return any(p in SENSITIVE_PATHS for p in parts)


def _has_meaningful_data(value: Any) -> bool:
    """Check if a value contains non-default data."""
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    if isinstance(value, bool) and value is False:
        return False
    if isinstance(value, dict):
        return any(_has_meaningful_data(v) for v in value.values())
    if isinstance(value, list) and len(value) == 0:
        return False
    return True


def _mask_value(value: Any) -> str:
    """Mask a sensitive value for display."""
    if isinstance(value, str) and len(value) > 8:
        return value[:4] + "****" + value[-2:]
    return repr(value)


def _set_nested(d: dict, path: str, value: Any) -> None:
    """Set a value at a dotted path in a nested dict."""
    keys = path.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _delete_nested(d: dict, path: str) -> None:
    """Delete a key at a dotted path in a nested dict."""
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            return
        d = d[key]
    d.pop(keys[-1], None)


def migrate_config(config_path: Path) -> MigrationResult:
    """Migrate an existing config file to the current schema.

    Returns a MigrationResult with confirmation removals NOT yet applied to data.
    """
    with open(config_path) as f:
        raw = json.load(f)
    existing = convert_keys(raw)

    default = Config().model_dump()
    added, removed = _deep_diff(existing, default)

    result = MigrationResult(data=existing, added=dict(added))

    # Apply additions (safe — just new defaults)
    for path, value in added.items():
        _set_nested(existing, path, value)

    # Handle removals
    for path, value in removed.items():
        if _has_meaningful_data(value):
            result.needs_confirm[path] = value
        else:
            result.auto_removed[path] = value
            _delete_nested(existing, path)

    return result


def migrate_credentials(creds_path: Path) -> MigrationResult:
    """Migrate an existing credentials file to the current schema.

    Returns a MigrationResult with confirmation removals NOT yet applied to data.
    """
    with open(creds_path) as f:
        raw = json.load(f)
    existing = convert_keys(raw)

    default = Credentials().model_dump()
    added, removed = _deep_diff(existing, default)

    result = MigrationResult(data=existing, added=dict(added))

    for path, value in added.items():
        _set_nested(existing, path, value)

    for path, value in removed.items():
        if _has_meaningful_data(value):
            result.needs_confirm[path] = value
        else:
            result.auto_removed[path] = value
            _delete_nested(existing, path)

    return result


def run_startup_migration(console: Console) -> bool:
    """Run config + credentials migration at gateway startup.

    Returns True to proceed, False to abort.
    """
    config_path = get_config_path()
    creds_path = get_credentials_path()

    config_exists = config_path.exists()
    creds_exists = creds_path.exists()

    if not config_exists and not creds_exists:
        return True

    # Collect results
    config_result = migrate_config(config_path) if config_exists else None
    creds_result = migrate_credentials(creds_path) if creds_exists else None

    results = [r for r in (config_result, creds_result) if r is not None]

    if not any(r.has_changes for r in results):
        return True

    # Merge needs_confirm across both files
    all_confirm: dict[str, Any] = {}
    for r in results:
        all_confirm.update(r.needs_confirm)

    # If only safe changes, apply and save silently
    if not all_confirm:
        _save_results(config_result, config_path, creds_result, creds_path)
        return True

    # Prompt for confirmations
    console.print("\n[yellow]Config migration: the following fields will be removed[/yellow]\n")
    for path, value in all_confirm.items():
        display = _mask_value(value) if _is_sensitive(path) else repr(value)
        console.print(f"  {path}: {display}")
    console.print()

    if not typer.confirm("Continue?", default=False):
        return False

    # Apply confirmed removals
    for r in results:
        for path in r.needs_confirm:
            _delete_nested(r.data, path)

    _save_results(config_result, config_path, creds_result, creds_path)
    return True


def _save_results(
    config_result: MigrationResult | None,
    config_path: Path,
    creds_result: MigrationResult | None,
    creds_path: Path,
) -> None:
    """Validate and save migrated config/credentials."""
    if config_result and config_result.has_changes:
        config = Config.model_validate(config_result.data)
        save_config(config, config_path)

    if creds_result and creds_result.has_changes:
        creds = Credentials.model_validate(creds_result.data)
        save_credentials(creds, creds_path)
