"""Tests for transcription providers and factory."""

import pytest

from ragnarbot.auth.credentials import ServicesCredentials
from ragnarbot.providers.transcription import (
    ElevenLabsTranscriptionProvider,
    GroqTranscriptionProvider,
    TranscriptionError,
    TranscriptionProvider,
    create_transcription_provider,
)


class TestTranscriptionError:
    def test_short_message(self):
        err = TranscriptionError("API error", "status 401")
        assert err.short_message == "API error"
        assert err.detail == "status 401"
        assert str(err) == "status 401"

    def test_short_message_only(self):
        err = TranscriptionError("file not found")
        assert err.short_message == "file not found"
        assert str(err) == "file not found"


class TestGroqProvider:
    def test_is_transcription_provider(self):
        provider = GroqTranscriptionProvider(api_key="test-key")
        assert isinstance(provider, TranscriptionProvider)

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        provider = GroqTranscriptionProvider(api_key="test-key")
        with pytest.raises(TranscriptionError, match="file not found"):
            await provider.transcribe(tmp_path / "nonexistent.ogg")


class TestElevenLabsProvider:
    def test_is_transcription_provider(self):
        provider = ElevenLabsTranscriptionProvider(api_key="test-key")
        assert isinstance(provider, TranscriptionProvider)

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        provider = ElevenLabsTranscriptionProvider(api_key="test-key")
        with pytest.raises(TranscriptionError, match="file not found"):
            await provider.transcribe(tmp_path / "nonexistent.ogg")


class TestFactory:
    def test_groq_with_key(self):
        services = ServicesCredentials()
        services.groq.api_key = "gsk-test"
        provider = create_transcription_provider("groq", services)
        assert isinstance(provider, GroqTranscriptionProvider)

    def test_elevenlabs_with_key(self):
        services = ServicesCredentials()
        services.elevenlabs.api_key = "xi-test"
        provider = create_transcription_provider("elevenlabs", services)
        assert isinstance(provider, ElevenLabsTranscriptionProvider)

    def test_groq_without_key_returns_none(self):
        services = ServicesCredentials()
        provider = create_transcription_provider("groq", services)
        assert provider is None

    def test_elevenlabs_without_key_returns_none(self):
        services = ServicesCredentials()
        provider = create_transcription_provider("elevenlabs", services)
        assert provider is None

    def test_none_provider(self):
        services = ServicesCredentials()
        provider = create_transcription_provider("none", services)
        assert provider is None

    def test_unknown_provider(self):
        services = ServicesCredentials()
        provider = create_transcription_provider("whisperx", services)
        assert provider is None
