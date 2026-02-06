"""Configuration loading utilities."""

import json
import os
from pathlib import Path
from typing import Any

from ragnarbot.config.schema import Config


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".ragnarbot" / "config.json"


def get_data_dir() -> Path:
    """Get the ragnarbot data directory."""
    from ragnarbot.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Automatically migrates credentials from config.json to credentials.json
    on first load if credentials.json doesn't exist yet.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            # Auto-migrate credentials on first load
            from ragnarbot.auth.credentials import get_credentials_path
            creds_path = get_credentials_path()
            if not creds_path.exists():
                migrate_credentials_from_config(path, creds_path)

            # Read config (possibly cleaned by migration)
            with open(path) as f:
                data = json.load(f)

            return Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def migrate_credentials_from_config(config_path: Path, creds_path: Path) -> None:
    """Move secrets from old config.json to credentials.json, strip from config.

    Called automatically when credentials.json doesn't exist but config.json
    may contain api keys from the old format.
    """
    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    data = convert_keys(raw)
    creds_data: dict[str, Any] = {"providers": {}, "services": {}, "channels": {}}
    changed = False

    # Extract provider api keys
    providers = data.get("providers", {})
    for name in ("anthropic", "openai", "gemini"):
        provider = providers.get(name, {})
        api_key = provider.get("api_key", "")
        if api_key:
            creds_data["providers"][name] = {"auth_method": "api_key", "api_key": api_key}
            changed = True

    # Extract transcription api key
    transcription = data.get("transcription", {})
    trans_key = transcription.get("api_key", "")
    if trans_key:
        creds_data["services"]["transcription"] = {"api_key": trans_key}
        changed = True

    # Extract web search api key
    tools = data.get("tools", {})
    web = tools.get("web", {})
    search = web.get("search", {})
    search_key = search.get("api_key", "")
    if search_key:
        creds_data["services"]["web_search"] = {"api_key": search_key}
        changed = True

    # Extract telegram bot token
    channels = data.get("channels", {})
    telegram = channels.get("telegram", {})
    tg_token = telegram.get("token", "")
    if tg_token:
        creds_data["channels"]["telegram"] = {"bot_token": tg_token}
        changed = True

    # Always strip removed fields from config.json (transcription, secrets)
    config_dirty = False
    for name in ("anthropic", "openai", "gemini"):
        provider = raw.get("providers", {}).get(name, {})
        if provider.pop("apiKey", None) is not None or provider.pop("api_key", None) is not None:
            config_dirty = True
    if raw.pop("transcription", None) is not None:
        config_dirty = True
    search_raw = raw.get("tools", {}).get("web", {}).get("search", {})
    if search_raw.pop("apiKey", None) is not None or search_raw.pop("api_key", None) is not None:
        config_dirty = True
    telegram_raw = raw.get("channels", {}).get("telegram", {})
    if telegram_raw.pop("token", None) is not None:
        config_dirty = True

    if config_dirty:
        with open(config_path, "w") as f:
            json.dump(raw, f, indent=2)

    # Write credentials.json if there were actual secrets to migrate
    if changed:
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        camel_creds = convert_to_camel(creds_data)
        with open(creds_path, "w") as f:
            json.dump(camel_creds, f, indent=2)
        os.chmod(creds_path, 0o600)
        print(f"Migrated credentials to {creds_path}")


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
