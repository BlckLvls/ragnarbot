"""Voice transcription providers (Groq Whisper, ElevenLabs Scribe, OpenAI GPT-4o)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from loguru import logger

from ragnarbot.auth.credentials import Credentials

# Maps a transcription provider value to the concrete OpenAI model id.
OPENAI_TRANSCRIPTION_MODELS = {
    "openai-gpt-4o-transcribe": "gpt-4o-transcribe",
    "openai-gpt-4o-mini-transcribe": "gpt-4o-mini-transcribe",
}


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


class OpenAITranscriptionProvider(TranscriptionProvider):
    """OpenAI GPT-4o transcription (gpt-4o-transcribe / gpt-4o-mini-transcribe)."""

    def __init__(self, api_key: str, model: str = "gpt-4o-transcribe"):
        self.api_key = api_key
        self.model = model
        self.api_url = "https://api.openai.com/v1/audio/transcriptions"

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
                        files={"file": (path.name, f), "model": (None, self.model)},
                        data={"response_format": "json"},
                        timeout=60.0,
                    )
                response.raise_for_status()
                text = response.json().get("text", "").strip()
                if not text:
                    raise TranscriptionError("empty response", "OpenAI returned empty text")
                return text
        except TranscriptionError:
            raise
        except httpx.HTTPStatusError as e:
            detail = f"OpenAI API {e.response.status_code}: {e.response.text[:200]}"
            logger.error(detail)
            raise TranscriptionError("API error", detail) from e
        except Exception as e:
            logger.error(f"OpenAI transcription error: {e}")
            raise TranscriptionError("transcription failed", str(e)) from e


def create_transcription_provider(
    provider_name: str,
    credentials: Credentials,
) -> TranscriptionProvider | None:
    """Factory: create a transcription provider by name, or None if disabled."""
    services = credentials.services

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

    if provider_name in OPENAI_TRANSCRIPTION_MODELS:
        # OpenAI transcription reuses the LLM provider key (shared token store).
        key = credentials.providers.openai.api_key
        if not key:
            logger.warning("OpenAI transcription selected but no OpenAI API key configured")
            return None
        return OpenAITranscriptionProvider(key, OPENAI_TRANSCRIPTION_MODELS[provider_name])

    return None
