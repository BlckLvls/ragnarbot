"""Tests for transcription providers and factory."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ragnarbot.auth.credentials import Credentials
from ragnarbot.providers.transcription import (
    ElevenLabsTranscriptionProvider,
    GroqTranscriptionProvider,
    OpenAITranscriptionProvider,
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


class TestOpenAIProvider:
    def test_is_transcription_provider(self):
        provider = OpenAITranscriptionProvider(api_key="sk-test")
        assert isinstance(provider, TranscriptionProvider)

    def test_default_model(self):
        provider = OpenAITranscriptionProvider(api_key="sk-test")
        assert provider.model == "gpt-4o-transcribe"

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        provider = OpenAITranscriptionProvider(api_key="sk-test")
        with pytest.raises(TranscriptionError, match="file not found"):
            await provider.transcribe(tmp_path / "nonexistent.ogg")

    @pytest.mark.asyncio
    async def test_retries_transient_transport_disconnect_once(self, tmp_path):
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"audio")
        provider = OpenAITranscriptionProvider(api_key="sk-test")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"text": "hello"}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=[
            httpx.RemoteProtocolError("Server disconnected without sending a response"),
            response,
        ])

        with (
            patch("ragnarbot.providers.transcription.httpx.AsyncClient", return_value=client),
            patch("ragnarbot.providers.transcription.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            assert await provider.transcribe(audio_path) == "hello"

        assert client.post.await_count == 2
        sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_does_not_retry_http_status_errors(self, tmp_path):
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"audio")
        provider = OpenAITranscriptionProvider(api_key="sk-test")

        request = httpx.Request("POST", provider.api_url)
        response = httpx.Response(401, request=request, text="unauthorized")
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(return_value=response)

        with patch(
            "ragnarbot.providers.transcription.httpx.AsyncClient",
            return_value=client,
        ):
            with pytest.raises(TranscriptionError, match="OpenAI API 401"):
                await provider.transcribe(audio_path)

        client.post.assert_awaited_once()


class TestFactory:
    def test_groq_with_key(self):
        creds = Credentials()
        creds.services.groq.api_key = "gsk-test"
        provider = create_transcription_provider("groq", creds)
        assert isinstance(provider, GroqTranscriptionProvider)

    def test_elevenlabs_with_key(self):
        creds = Credentials()
        creds.services.elevenlabs.api_key = "xi-test"
        provider = create_transcription_provider("elevenlabs", creds)
        assert isinstance(provider, ElevenLabsTranscriptionProvider)

    def test_openai_transcribe_with_key(self):
        creds = Credentials()
        creds.providers.openai.api_key = "sk-test"
        provider = create_transcription_provider("openai-gpt-4o-transcribe", creds)
        assert isinstance(provider, OpenAITranscriptionProvider)
        assert provider.model == "gpt-4o-transcribe"

    def test_openai_mini_transcribe_with_key(self):
        creds = Credentials()
        creds.providers.openai.api_key = "sk-test"
        provider = create_transcription_provider("openai-gpt-4o-mini-transcribe", creds)
        assert isinstance(provider, OpenAITranscriptionProvider)
        assert provider.model == "gpt-4o-mini-transcribe"

    def test_groq_without_key_returns_none(self):
        creds = Credentials()
        provider = create_transcription_provider("groq", creds)
        assert provider is None

    def test_elevenlabs_without_key_returns_none(self):
        creds = Credentials()
        provider = create_transcription_provider("elevenlabs", creds)
        assert provider is None

    def test_openai_without_key_returns_none(self):
        creds = Credentials()
        provider = create_transcription_provider("openai-gpt-4o-transcribe", creds)
        assert provider is None

    def test_none_provider(self):
        creds = Credentials()
        provider = create_transcription_provider("none", creds)
        assert provider is None

    def test_unknown_provider(self):
        creds = Credentials()
        provider = create_transcription_provider("whisperx", creds)
        assert provider is None
