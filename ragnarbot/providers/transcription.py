"""Voice transcription providers (Groq Whisper, ElevenLabs Scribe)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from loguru import logger

from ragnarbot.auth.credentials import ServicesCredentials


class TranscriptionError(Exception):
    """Transcription failure with a user-facing short message."""

    def __init__(self, short_message: str, detail: str = ""):
        self.short_message = short_message
        self.detail = detail
        super().__init__(detail or short_message)


class TranscriptionProvider(ABC):
    """Abstract base for voice transcription providers."""

    @abstractmethod
    async def transcribe(self, file_path: str | Path) -> str:
        """Transcribe an audio file. Raises TranscriptionError on failure."""


class GroqTranscriptionProvider(TranscriptionProvider):
    """Groq Whisper v3 Turbo transcription."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            raise TranscriptionError("file not found", f"Audio file not found: {path}")

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    response = await client.post(
                        self.api_url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        files={"file": (path.name, f), "model": (None, "whisper-large-v3-turbo")},
                        data={"response_format": "json"},
                        timeout=60.0,
                    )
                response.raise_for_status()
                text = response.json().get("text", "").strip()
                if not text:
                    raise TranscriptionError("empty response", "Groq returned empty text")
                return text
        except TranscriptionError:
            raise
        except httpx.HTTPStatusError as e:
            detail = f"Groq API {e.response.status_code}: {e.response.text[:200]}"
            logger.error(detail)
            raise TranscriptionError("API error", detail) from e
        except Exception as e:
            logger.error(f"Groq transcription error: {e}")
            raise TranscriptionError("transcription failed", str(e)) from e


class ElevenLabsTranscriptionProvider(TranscriptionProvider):
    """ElevenLabs Scribe v2 transcription."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api_url = "https://api.elevenlabs.io/v1/speech-to-text"

    async def transcribe(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            raise TranscriptionError("file not found", f"Audio file not found: {path}")

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    response = await client.post(
                        self.api_url,
                        headers={"xi-api-key": self.api_key},
                        files={"file": (path.name, f)},
                        data={"model_id": "scribe_v2", "tag_audio_events": "false"},
                        timeout=60.0,
                    )
                response.raise_for_status()
                text = response.json().get("text", "").strip()
                if not text:
                    raise TranscriptionError("empty response", "ElevenLabs returned empty text")
                return text
        except TranscriptionError:
            raise
        except httpx.HTTPStatusError as e:
            detail = f"ElevenLabs API {e.response.status_code}: {e.response.text[:200]}"
            logger.error(detail)
            raise TranscriptionError("API error", detail) from e
        except Exception as e:
            logger.error(f"ElevenLabs transcription error: {e}")
            raise TranscriptionError("transcription failed", str(e)) from e


def create_transcription_provider(
    provider_name: str,
    services: ServicesCredentials,
) -> TranscriptionProvider | None:
    """Factory: create a transcription provider by name, or None if disabled."""
    if provider_name == "groq":
        key = services.groq.api_key
        if not key:
            logger.warning("Groq transcription selected but no API key configured")
            return None
        return GroqTranscriptionProvider(key)

    if provider_name == "elevenlabs":
        key = services.elevenlabs.api_key
        if not key:
            logger.warning("ElevenLabs transcription selected but no API key configured")
            return None
        return ElevenLabsTranscriptionProvider(key)

    return None
