"""TUI onboarding wizard for ragnarbot."""

from rich.console import Console

from ragnarbot.cli.tui.components import QuitOnboardingError, clear_screen
from ragnarbot.cli.tui.screens import (
    auth_method_screen,
    daemon_screen,
    model_screen,
    provider_screen,
    summary_screen,
    telegram_screen,
    token_input_screen,
    voice_transcription_screen,
    web_search_screen,
)
from ragnarbot.config.providers import PROVIDERS, get_models, get_provider, supports_oauth


def run_onboarding(console: Console) -> None:
    """Run the full onboarding wizard."""
    try:
        _onboarding_loop(console)
    except QuitOnboardingError:
        clear_screen(console)
        console.print("\n  Setup cancelled.\n")
    except KeyboardInterrupt:
        clear_screen(console)
        console.print("\n  Setup cancelled.\n")


def _onboarding_loop(console: Console) -> None:
    """Main onboarding state machine with back navigation."""
    # State
    provider_idx: int | None = None
    auth_idx: int | None = None
    token: str | None = None
    model_idx: int | None = None
    telegram_token: str | None = None
    voice_provider: str = "none"
    voice_api_key: str = ""
    web_search_key: str = ""
    enable_daemon: bool | None = None

    step = 1  # 1=provider, 2=auth, 3=token, 4=model, 5=telegram, 6=voice, 7=web_search, 8=daemon, 9=summary

    while True:
        if step == 1:
            provider_idx = provider_screen(console)
            if provider_idx is None:
                # Quit from first screen
                raise QuitOnboardingError()
            step = 2

        elif step == 2:
            provider_id = PROVIDERS[provider_idx]["id"]
            if supports_oauth(provider_id):
                auth_idx = auth_method_screen(console, provider_id)
                if auth_idx is None:
                    step = 1
                    continue
            else:
                auth_idx = 1  # api_key
            step = 3

        elif step == 3:
            provider_id = PROVIDERS[provider_idx]["id"]
            auth_method = "oauth" if auth_idx == 0 else "api_key"
            token = token_input_screen(console, provider_id, auth_method)
            if token is None:
                # Go back to auth or provider
                if supports_oauth(provider_id):
                    step = 2
                else:
                    step = 1
                continue
            step = 4

        elif step == 4:
            provider_id = PROVIDERS[provider_idx]["id"]
            model_idx = model_screen(console, provider_id)
            if model_idx is None:
                step = 3
                continue
            step = 5

        elif step == 5:
            telegram_token = telegram_screen(console)
            if telegram_token is None:
                step = 4
                continue
            step = 6

        elif step == 6:
            result = voice_transcription_screen(console)
            if result is None:
                step = 5
                continue
            voice_provider, voice_api_key = result
            step = 7

        elif step == 7:
            web_search_key_result = web_search_screen(console)
            if web_search_key_result is None:
                step = 6
                continue
            web_search_key = web_search_key_result
            step = 8

        elif step == 8:
            daemon_idx = daemon_screen(console)
            if daemon_idx is None:
                step = 7
                continue
            enable_daemon = daemon_idx == 0
            step = 9

        elif step == 9:
            provider_id = PROVIDERS[provider_idx]["id"]
            provider = get_provider(provider_id)
            auth_method = "oauth" if auth_idx == 0 else "api_key"
            models = get_models(provider_id)
            model = models[model_idx]
            telegram_configured = bool(telegram_token)

            ok = summary_screen(
                console,
                provider["name"],
                auth_method,
                model["name"],
                telegram_configured,
                enable_daemon=enable_daemon,
                voice_provider=voice_provider,
                web_search_configured=bool(web_search_key),
            )
            if not ok:
                step = 8
                continue

            # Save everything
            _save_results(
                console=console,
                provider_id=provider_id,
                auth_method=auth_method,
                token=token,
                model_id=model["id"],
                telegram_token=telegram_token if telegram_configured else "",
                enable_daemon=enable_daemon,
                voice_provider=voice_provider,
                voice_api_key=voice_api_key,
                web_search_key=web_search_key,
            )
            return


def _save_results(
    console: Console,
    provider_id: str,
    auth_method: str,
    token: str,
    model_id: str,
    telegram_token: str,
    enable_daemon: bool = False,
    voice_provider: str = "none",
    voice_api_key: str = "",
    web_search_key: str = "",
) -> None:
    """Save onboarding results to config and credentials files."""
    from ragnarbot.auth.credentials import (
        get_credentials_path,
        load_credentials,
        save_credentials,
    )
    from ragnarbot.config.loader import get_config_path, load_config, save_config
    from ragnarbot.utils.helpers import get_workspace_path

    # Load existing or create new
    config = load_config()
    creds = load_credentials()

    # Update config
    config.agents.defaults.model = model_id
    config.agents.defaults.auth_method = auth_method

    # Update telegram
    if telegram_token:
        config.channels.telegram.enabled = True

    # Update voice transcription
    config.transcription.provider = voice_provider

    # Update daemon
    config.daemon.enabled = enable_daemon

    save_config(config)

    # Update credentials (targeted — only touch the selected provider)
    provider_creds = getattr(creds.providers, provider_id)
    if auth_method == "oauth":
        provider_creds.oauth_key = token
    else:
        provider_creds.api_key = token

    if telegram_token:
        creds.channels.telegram.bot_token = telegram_token

    if voice_api_key and voice_provider in ("groq", "elevenlabs"):
        getattr(creds.services, voice_provider).api_key = voice_api_key

    if web_search_key:
        creds.services.brave_search.api_key = web_search_key

    save_credentials(creds)

    # Ensure workspace exists
    workspace = get_workspace_path()

    # Create workspace templates if needed
    from ragnarbot.cli.commands import _create_workspace_templates
    _create_workspace_templates(workspace)

    # Install and start daemon if requested
    daemon_started = False
    if enable_daemon:
        try:
            from ragnarbot.daemon import get_manager
            manager = get_manager()
            manager.install()
            manager.start()
            daemon_started = True
        except Exception as e:
            console.print(f"\n  [yellow]Warning: Could not start daemon: {e}[/yellow]")
            console.print("  [yellow]You can start it manually: ragnarbot gateway start[/yellow]")

    clear_screen(console)
    console.print()
    console.print("  [green]Configuration saved![/green]")
    console.print()
    console.print(f"  Config:      {get_config_path()}")
    console.print(f"  Credentials: {get_credentials_path()}")
    console.print(f"  Workspace:   {workspace}")
    console.print()
    if daemon_started:
        console.print("  [green]Gateway is running![/green]")
        console.print()
        console.print("  [bold]Daemon commands:[/bold]")
        console.print("  [cyan]ragnarbot gateway start[/cyan]    Install and start daemon")
        console.print("  [cyan]ragnarbot gateway stop[/cyan]     Stop daemon")
        console.print("  [cyan]ragnarbot gateway restart[/cyan]  Restart daemon")
        console.print("  [cyan]ragnarbot gateway delete[/cyan]   Remove daemon from system")
        console.print("  [cyan]ragnarbot gateway status[/cyan]   Show daemon status")
    else:
        console.print("  [bold]Next steps:[/bold]")
        console.print("  Chat: [cyan]ragnarbot agent -m \"Hello!\"[/cyan]")
        console.print("  Start manually: [cyan]ragnarbot gateway[/cyan]")
        console.print("  Enable daemon:  [cyan]ragnarbot gateway start[/cyan]")
    console.print()
