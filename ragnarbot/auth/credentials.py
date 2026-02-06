"""Credentials schema and persistence (separate from config)."""

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


class ProviderCredentials(BaseModel):
    """Credentials for a single LLM provider."""
    api_key: str = ""
    oauth_key: str = ""


class ProvidersCredentials(BaseModel):
    """Credentials for all LLM providers."""
    anthropic: ProviderCredentials = Field(default_factory=ProviderCredentials)
    openai: ProviderCredentials = Field(default_factory=ProviderCredentials)
    gemini: ProviderCredentials = Field(default_factory=ProviderCredentials)


class ServiceCredential(BaseModel):
    """Credentials for a single service."""
    api_key: str = ""


class ServicesCredentials(BaseModel):
    """Credentials for external services."""
    transcription: ServiceCredential = Field(default_factory=ServiceCredential)
    web_search: ServiceCredential = Field(default_factory=ServiceCredential)


class ChannelCredential(BaseModel):
    """Credentials for a single channel."""
    bot_token: str = ""


class ChannelsCredentials(BaseModel):
    """Credentials for chat channels."""
    telegram: ChannelCredential = Field(default_factory=ChannelCredential)


class Credentials(BaseModel):
    """Root credentials model. Stored in ~/.ragnarbot/credentials.json with 0o600."""
    providers: ProvidersCredentials = Field(default_factory=ProvidersCredentials)
    services: ServicesCredentials = Field(default_factory=ServicesCredentials)
    channels: ChannelsCredentials = Field(default_factory=ChannelsCredentials)


def get_credentials_path() -> Path:
    """Get the default credentials file path."""
    return Path.home() / ".ragnarbot" / "credentials.json"


def load_credentials(creds_path: Path | None = None) -> Credentials:
    """Load credentials from file or return defaults."""
    from ragnarbot.config.loader import convert_keys

    path = creds_path or get_credentials_path()

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            return Credentials.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load credentials from {path}: {e}")
            print("Using default credentials.")

    return Credentials()


def save_credentials(creds: Credentials, creds_path: Path | None = None) -> None:
    """Save credentials to file with 0o600 permissions."""
    from ragnarbot.config.loader import convert_to_camel

    path = creds_path or get_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = creds.model_dump()
    data = convert_to_camel(data)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    os.chmod(path, 0o600)
