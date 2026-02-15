"""Onboarding screen functions."""

import httpx
from rich.console import Console

from ragnarbot.cli.tui.components import info_screen, select_menu, text_input
from ragnarbot.config.providers import PROVIDERS, get_models, get_provider


def provider_screen(console: Console) -> int | None:
    """Select LLM provider. Returns index or None (back)."""
    options = [(p["name"], p["description"]) for p in PROVIDERS]
    return select_menu(
        console,
        "Choose your LLM provider",
        options,
        subtitle="Step 1 of 8",
        back_label="Quit",
    )


def auth_method_screen(console: Console, provider_id: str) -> int | None:
    """Select auth method. Returns index (0=oauth, 1=api_key) or None."""
    oauth_descriptions = {
        "anthropic": "Recommended — uses Claude CLI token",
        "gemini": "Sign in with Google account",
        "openai": "Sign in with OpenAI account",
    }
    oauth_desc = oauth_descriptions.get(provider_id, "OAuth authentication")
    options = [
        ("OAuth", oauth_desc),
        ("API Key", "Traditional API key authentication"),
    ]
    return select_menu(
        console,
        "Choose authentication method",
        options,
        subtitle=f"Step 2 of 8 — {get_provider(provider_id)['name']}",
    )


def token_input_screen(
    console: Console, provider_id: str, auth_method: str
) -> str | None:
    """Input token/key. Returns value or None (back)."""
    provider = get_provider(provider_id)

    if auth_method == "oauth":
        hint = 'Run "claude setup-token" in another terminal, then paste the token here'
        prompt = "OAuth token"
    else:
        hint = f"Get your API key at: {provider['api_key_url']}"
        prompt = "API key"

    return text_input(
        console,
        "Enter your credentials",
        prompt,
        hint=hint,
        secret=False,
        subtitle=f"Step 3 of 8 — {provider['name']}",
    )


def model_screen(console: Console, provider_id: str) -> int | None:
    """Select model. Returns index or None (back)."""
    models = get_models(provider_id)
    options = [(m["name"], m["description"]) for m in models]
    provider = get_provider(provider_id)
    return select_menu(
        console,
        "Choose your default model",
        options,
        subtitle=f"Step 4 of 8 — {provider['name']}",
    )


def telegram_screen(console: Console) -> str | None:
    """Telegram bot setup. Returns token, "" (skip), or None (back)."""
    token = text_input(
        console,
        "Telegram bot setup",
        "Bot token",
        hint=(
            "1. Open Telegram, search @BotFather\n"
            "  2. Send /newbot and follow the prompts\n"
            "  3. Copy the bot token and paste it here\n"
            "\n"
            "  Press Enter with empty input to skip"
        ),
        allow_empty=True,
        subtitle="Step 5 of 8 — Optional",
    )

    if token is None or token == "":
        return token

    # Validate token via Telegram API
    return _validate_telegram_token(console, token)


def _validate_telegram_token(console: Console, token: str) -> str | None:
    """Validate a Telegram bot token. Returns token if valid, None to retry."""
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            bot = data["result"]
            bot_name = bot.get("first_name", "Unknown")
            bot_username = bot.get("username", "unknown")
            info_screen(
                console,
                "Bot verified",
                [
                    "[green]Bot connected successfully![/green]",
                    "",
                    f"Name: [bold]{bot_name}[/bold]",
                    f"Username: @{bot_username}",
                ],
                subtitle="Step 5 of 8",
            )
            return token
        else:
            info_screen(
                console,
                "Invalid token",
                [
                    "[red]The bot token was not accepted by Telegram.[/red]",
                    "",
                    f"Error: {data.get('description', 'Unknown error')}",
                    "",
                    "Press Enter to try again, or Esc to skip.",
                ],
                subtitle="Step 5 of 8",
            )
            return None
    except httpx.RequestError as e:
        info_screen(
            console,
            "Connection error",
            [
                "[red]Could not connect to Telegram API.[/red]",
                "",
                f"Error: {e}",
                "",
                "Press Enter to try again, or Esc to skip.",
            ],
            subtitle="Step 5 of 8",
        )
        return None


def voice_transcription_screen(console: Console) -> tuple[str, str] | None:
    """Voice transcription setup. Returns (provider, api_key), ("none","") for skip, or None (back)."""
    voice_providers = [
        ("ElevenLabs (Scribe v2)", "Best quality, multilingual — recommended"),
        ("Groq (Whisper v3 Turbo)", "Fast and free"),
        ("Skip", "Disable voice transcription"),
    ]
    idx = select_menu(
        console,
        "Voice transcription provider",
        voice_providers,
        subtitle="Step 6 of 8 — Optional",
    )
    if idx is None:
        return None
    if idx == 2:
        return ("none", "")

    provider_id = "elevenlabs" if idx == 0 else "groq"
    provider_label = "ElevenLabs" if idx == 0 else "Groq"

    api_key = text_input(
        console,
        f"{provider_label} API key",
        "API key",
        hint="Paste your API key and press Enter",
        secret=False,
        subtitle=f"Step 6 of 8 — {provider_label}",
    )
    if api_key is None:
        return None
    return (provider_id, api_key)


def web_search_screen(console: Console) -> tuple[str, str] | None:
    """Web search engine setup.

    Returns (engine, api_key) tuple, or None (back).
    engine is "brave", "duckduckgo", or "none".
    """
    engines = [
        ("Brave Search", "Best quality, requires free API key"),
        ("DuckDuckGo", "No API key needed, slightly slower"),
        ("Skip", "Disable web search"),
    ]
    idx = select_menu(
        console,
        "Web search engine",
        engines,
        subtitle="Step 7 of 8 — Optional",
    )
    if idx is None:
        return None
    if idx == 2:
        return ("none", "")
    if idx == 1:
        return ("duckduckgo", "")

    # Brave selected — ask for API key
    api_key = text_input(
        console,
        "Brave Search API key",
        "API key",
        hint=(
            "Get a free API key at: https://brave.com/search/api/\n"
            "\n"
            "  Press Enter with empty input to skip"
        ),
        allow_empty=True,
        subtitle="Step 7 of 8 — Brave Search",
    )
    if api_key is None:
        return None
    if api_key == "":
        return ("none", "")
    return ("brave", api_key)


def daemon_screen(console: Console) -> int | None:
    """Auto-start daemon setup. Returns 0 (yes) or 1 (no), None for back."""
    import sys

    if sys.platform not in ("darwin", "linux"):
        info_screen(
            console,
            "Auto-start not available",
            [
                "[yellow]Daemon management is not supported on this platform.[/yellow]",
                "",
                "You can run the gateway manually with:",
                "  [cyan]ragnarbot gateway[/cyan]",
            ],
            subtitle="Step 7 of 8",
        )
        return 1  # "no" — continue without daemon

    platform_name = "launchd" if sys.platform == "darwin" else "systemd"
    options = [
        ("Yes, enable auto-start", f"Starts on boot, auto-restarts on crash (uses {platform_name})"),
        ("No, I'll start manually", "Use 'ragnarbot gateway' when needed"),
    ]
    return select_menu(
        console,
        "Enable auto-start?",
        options,
        subtitle="Step 7 of 8",
    )


def summary_screen(
    console: Console,
    provider_name: str,
    auth_method: str,
    model_name: str,
    telegram_configured: bool,
    enable_daemon: bool = False,
    voice_provider: str = "none",
    search_engine: str = "none",
) -> bool:
    """Show summary of configured values. Returns True on Enter."""
    voice_label = {"groq": "Groq", "elevenlabs": "ElevenLabs", "none": "Skipped"}
    search_label = {"brave": "Brave Search", "duckduckgo": "DuckDuckGo", "none": "Skipped"}
    lines = [
        "[bold]Configuration summary:[/bold]",
        "",
        f"  Provider:       [cyan]{provider_name}[/cyan]",
        f"  Auth:           [cyan]{auth_method}[/cyan]",
        f"  Model:          [cyan]{model_name}[/cyan]",
        f"  Telegram:       [cyan]{'Enabled' if telegram_configured else 'Skipped'}[/cyan]",
        f"  Transcription:  [cyan]{voice_label.get(voice_provider, voice_provider)}[/cyan]",
        f"  Web search:     [cyan]{search_label.get(search_engine, search_engine)}[/cyan]",
        f"  Auto-start:     [cyan]{'Enabled' if enable_daemon else 'Manual'}[/cyan]",
        "",
        "[green]Press Enter to save and finish.[/green]",
    ]
    return info_screen(console, "Setup complete", lines, subtitle="Review")
