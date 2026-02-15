"""Secrets management helpers for the config tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ragnarbot.auth.credentials import Credentials, load_credentials
from ragnarbot.config.path_utils import get_all_paths, get_by_path, set_by_path


@dataclass(frozen=True)
class ConfigDependency:
    """Declares that a config value requires certain credentials to be set."""
    config_path: str
    value_match: str
    match_mode: str  # "prefix" or "exact"
    creds_paths: list[str] = field(default_factory=list)
    error_msg: str = ""


_MODEL_DEPENDENCIES = [
    ("anthropic/", ["providers.anthropic.api_key", "providers.anthropic.oauth_key"],
     "requires secrets.providers.anthropic (api_key or oauth_key)"),
    ("gemini/", ["providers.gemini.api_key", "providers.gemini.oauth_key"],
     "requires secrets.providers.gemini (api_key or oauth_key)"),
    ("openai/", ["providers.openai.api_key", "providers.openai.oauth_key"],
     "requires secrets.providers.openai (api_key or oauth_key)"),
    ("openrouter/", ["providers.openrouter.api_key"],
     "requires secrets.providers.openrouter.api_key"),
]

CONFIG_DEPENDENCIES = [
    ConfigDependency(path, prefix, "prefix", creds, msg)
    for path in ("agents.defaults.model", "agents.fallback.model")
    for prefix, creds, msg in _MODEL_DEPENDENCIES
] + [
    ConfigDependency(
        "tools.web.search.engine", "brave", "exact",
        ["services.brave_search.api_key"],
        "requires secrets.services.brave_search.api_key",
    ),
    ConfigDependency(
        "transcription.provider", "groq", "exact",
        ["services.groq.api_key"],
        "requires secrets.services.groq.api_key",
    ),
    ConfigDependency(
        "transcription.provider", "elevenlabs", "exact",
        ["services.elevenlabs.api_key"],
        "requires secrets.services.elevenlabs.api_key",
    ),
]


def check_config_dependency(config_path: str, new_value: str) -> str | None:
    """Check if a config change requires credentials that aren't set.

    Returns an error message if a required credential is missing, None if OK.
    """
    creds = load_credentials()
    str_value = str(new_value)

    for dep in CONFIG_DEPENDENCIES:
        if dep.config_path != config_path:
            continue

        matches = False
        if dep.match_mode == "prefix":
            matches = str_value.startswith(dep.value_match)
        elif dep.match_mode == "exact":
            matches = str_value == dep.value_match

        if not matches:
            continue

        # Check if at least one credential path is set (OR logic)
        any_set = False
        for cp in dep.creds_paths:
            try:
                val = get_by_path(creds, cp)
                if val:
                    any_set = True
                    break
            except ValueError:
                continue

        if not any_set:
            return f"Blocked: '{config_path}={str_value}' {dep.error_msg}"

    return None


def secrets_schema(creds: Credentials, filter_path: str | None = None) -> str:
    """Show secrets schema with set/not-set status indicators."""
    all_paths = get_all_paths(creds)
    lines = []

    for p in sorted(all_paths.keys()):
        if p == "extra":
            # Skip the dict itself; handled below
            continue
        full = f"secrets.{p}"
        if filter_path and not full.startswith(filter_path):
            continue
        val = all_paths[p]
        status = "[set \u2713]" if val else "[not set \u2717]"
        lines.append(f"{full} {status}")

    # Extra keys
    for key in sorted(creds.extra.keys()):
        full = f"secrets.extra.{key}"
        if filter_path and not full.startswith(filter_path):
            continue
        status = "[set \u2713]" if creds.extra[key] else "[not set \u2717]"
        lines.append(f"{full} {status}")

    if not lines:
        return f"No secrets matching '{filter_path}'" if filter_path else "No secrets found"
    return "\n".join(lines)


def secrets_get(creds: Credentials, creds_path: str) -> str:
    """Get a secret value by path (after stripping 'secrets.' prefix)."""
    if creds_path.startswith("extra."):
        key = creds_path[6:]
        if key not in creds.extra:
            return f"Error: key '{key}' not found in secrets.extra"
        return json.dumps({
            "path": f"secrets.{creds_path}",
            "value": creds.extra[key],
            "type": "str",
            "reload": "warm",
        })

    try:
        value = get_by_path(creds, creds_path)
        return json.dumps({
            "path": f"secrets.{creds_path}",
            "value": value,
            "type": type(value).__name__,
            "reload": "warm",
        })
    except ValueError as e:
        return f"Error: {e}"


def secrets_set(
    creds: Credentials, creds_path: str, value: str,
) -> tuple[Credentials, str]:
    """Set a secret value. Returns (modified creds, JSON result)."""
    if creds_path.startswith("extra."):
        key = creds_path[6:]
        creds.extra[key] = value
        return creds, json.dumps({
            "path": f"secrets.{creds_path}",
            "status": "saved",
            "detail": "Restart to apply.",
        })

    try:
        set_by_path(creds, creds_path, value)
        return creds, json.dumps({
            "path": f"secrets.{creds_path}",
            "status": "saved",
            "detail": "Restart to apply.",
        })
    except ValueError as e:
        return creds, f"Error: {e}"


def secrets_list(creds: Credentials) -> str:
    """List all secrets with masked values."""
    all_paths = get_all_paths(creds)
    lines = []

    for p in sorted(all_paths.keys()):
        if p == "extra":
            continue
        val = all_paths[p]
        display = "****" if val else "[not set]"
        lines.append(f"secrets.{p} = {display}")

    for key in sorted(creds.extra.keys()):
        val = creds.extra[key]
        display = "****" if val else "[not set]"
        lines.append(f"secrets.extra.{key} = {display}")

    return "\n".join(lines)
