"""CLI commands for ragnarbot."""

import asyncio
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ragnarbot import __logo__, __version__

app = typer.Typer(
    name="ragnarbot",
    help=f"{__logo__} ragnarbot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def _create_provider(model: str, auth_method: str, creds):
    """Create an LLM provider from model string and auth method."""
    from ragnarbot.providers.litellm_provider import LiteLLMProvider

    provider_name = model.split("/")[0] if "/" in model else "anthropic"
    provider_creds = getattr(creds.providers, provider_name, None)

    if auth_method == "oauth":
        if provider_name == "anthropic":
            oauth_token = provider_creds.oauth_key if provider_creds else None
            from ragnarbot.providers.anthropic_provider import AnthropicProvider
            return AnthropicProvider(oauth_token=oauth_token, default_model=model)
        elif provider_name == "gemini":
            from ragnarbot.providers.gemini_provider import GeminiCodeAssistProvider
            return GeminiCodeAssistProvider(default_model=model)
        elif provider_name == "openai":
            from ragnarbot.providers.openai_chatgpt_provider import OpenAIChatGPTProvider
            return OpenAIChatGPTProvider(default_model=model)

    api_key = provider_creds.api_key if provider_creds else None
    return LiteLLMProvider(api_key=api_key, default_model=model)


def _validate_auth(config, creds):
    """Validate auth configuration before provider creation.

    Returns error message string or None if OK.
    """
    from ragnarbot.config.schema import OAUTH_SUPPORTED_PROVIDERS

    model = config.agents.defaults.model
    provider_name = model.split("/")[0] if "/" in model else "anthropic"
    auth_method = config.agents.defaults.auth_method

    if auth_method not in ("api_key", "oauth"):
        return f"Unknown auth method: {auth_method}"

    if auth_method == "oauth" and provider_name not in OAUTH_SUPPORTED_PROVIDERS:
        return (
            f"OAuth is not supported for provider '{provider_name}'. "
            f"Supported: {', '.join(OAUTH_SUPPORTED_PROVIDERS)}"
        )

    if auth_method == "oauth":
        if provider_name == "gemini":
            from ragnarbot.auth.gemini_oauth import is_authenticated
            if not is_authenticated():
                return "Gemini OAuth not configured. Run: ragnarbot oauth gemini"
        elif provider_name == "openai":
            from ragnarbot.auth.openai_oauth import is_authenticated
            if not is_authenticated():
                return "OpenAI OAuth not configured. Run: ragnarbot oauth openai"
        else:
            # Anthropic — uses credentials file
            provider_creds = getattr(creds.providers, provider_name, None)
            if not provider_creds or not provider_creds.oauth_key:
                return f"No OAuth token for '{provider_name}'. Run: claude setup-token"
        return None

    # api_key path
    provider_creds = getattr(creds.providers, provider_name, None)
    if not provider_creds:
        return f"No credentials configured for provider '{provider_name}'"

    if not provider_creds.api_key:
        return f"No API key configured for '{provider_name}'. Set it in ~/.ragnarbot/credentials.json"

    return None


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} ragnarbot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """ragnarbot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Interactive setup wizard for ragnarbot."""
    from ragnarbot.cli.tui import run_onboarding
    run_onboarding(console)


# ============================================================================
# OAuth Commands
# ============================================================================

oauth_app = typer.Typer(help="OAuth authentication")
app.add_typer(oauth_app, name="oauth")


@oauth_app.command("gemini")
def oauth_gemini():
    """Authenticate with Google Gemini via OAuth."""
    from ragnarbot.auth.gemini_oauth import authenticate
    success = authenticate(console)
    if not success:
        raise typer.Exit(1)


@oauth_app.command("openai")
def oauth_openai():
    """Authenticate with OpenAI via OAuth."""
    from ragnarbot.auth.openai_oauth import authenticate
    success = authenticate(console)
    if not success:
        raise typer.Exit(1)


@app.command()
def bootstrap():
    """Re-run the identity bootstrap protocol."""
    import shutil

    from ragnarbot.agent.context import DEFAULTS_DIR
    from ragnarbot.config.loader import load_config

    config = load_config()
    workspace = config.workspace_path

    # Remove .bootstrap_done marker so it won't be skipped
    done_marker = workspace / ".bootstrap_done"
    done_marker.unlink(missing_ok=True)

    # Copy BOOTSTRAP.md from defaults
    source = DEFAULTS_DIR / "BOOTSTRAP.md"
    target = workspace / "BOOTSTRAP.md"

    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        console.print("[green]✓[/green] Bootstrap protocol activated.")
        console.print("Start a conversation to begin the identity setup.")
    else:
        console.print("[red]Error: BOOTSTRAP.md template not found[/red]")
        raise typer.Exit(1)


def _create_workspace_templates(workspace: Path):
    """Copy default workspace files from workspace_defaults/ if missing."""
    import shutil
    from ragnarbot.agent.context import DEFAULTS_DIR

    for default_file in DEFAULTS_DIR.rglob("*"):
        if not default_file.is_file():
            continue
        rel = default_file.relative_to(DEFAULTS_DIR)
        target = workspace / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(default_file, target)
            console.print(f"  [dim]Created {rel}[/dim]")


# ============================================================================
# Gateway / Server
# ============================================================================

gateway_app = typer.Typer(help="Manage the ragnarbot gateway", invoke_without_command=True)
app.add_typer(gateway_app, name="gateway")


@gateway_app.callback()
def gateway_main(
    ctx: typer.Context,
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the ragnarbot gateway. Use subcommands to manage the daemon."""
    if ctx.invoked_subcommand is not None:
        return
    from ragnarbot.agent.loop import AgentLoop
    from ragnarbot.auth.credentials import load_credentials
    from ragnarbot.bus.queue import MessageBus
    from ragnarbot.channels.manager import ChannelManager
    from ragnarbot.config.loader import get_data_dir, load_config
    from ragnarbot.cron.service import CronService
    from ragnarbot.cron.types import CronJob
    from ragnarbot.heartbeat.service import HeartbeatService
    from ragnarbot.media.manager import MediaManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting ragnarbot gateway on port {port}...")

    from ragnarbot.daemon.resolve import resolve_path
    resolve_path()

    from ragnarbot.config.migration import run_startup_migration
    if not run_startup_migration(console):
        raise typer.Exit(0)

    config = load_config()
    creds = load_credentials()

    # Create components
    bus = MessageBus()

    # Validate auth configuration
    error = _validate_auth(config, creds)
    if error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(1)

    provider = _create_provider(
        config.agents.defaults.model, config.agents.defaults.auth_method, creds,
    )

    # Apply config defaults to provider (base class starts with hardcoded values)
    provider.set_temperature(config.agents.defaults.temperature)
    provider.set_max_tokens(config.agents.defaults.max_tokens)

    # Fallback config
    fallback_config = config.agents.fallback

    # Service credentials
    brave_api_key = creds.services.brave_search.api_key or None
    search_engine = config.tools.web.search.engine

    # Create media manager
    media_dir = get_data_dir() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_manager = MediaManager(base_dir=media_dir)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        brave_api_key=brave_api_key,
        search_engine=search_engine,
        exec_config=config.tools.exec,
        cron_service=cron,
        stream_steps=config.agents.defaults.stream_steps,
        media_manager=media_manager,
        debounce_seconds=config.agents.defaults.debounce_seconds,
        max_context_tokens=config.agents.defaults.max_context_tokens,
        context_mode=config.agents.defaults.context_mode,
        heartbeat_interval_m=config.heartbeat.interval_m,
        fallback_model=fallback_config.model,
        fallback_config=fallback_config,
        provider_factory=lambda model, auth_method: _create_provider(model, auth_method, creds),
    )

    # Set cron callback (needs agent)
    def _format_schedule(schedule) -> str:
        if schedule.kind == "every" and schedule.every_ms:
            secs = schedule.every_ms // 1000
            if secs >= 3600:
                return f"every {secs // 3600}h"
            if secs >= 60:
                return f"every {secs // 60}m"
            return f"every {secs}s"
        if schedule.kind == "cron" and schedule.expr:
            return f"cron({schedule.expr})"
        if schedule.kind == "at":
            return "one-time"
        return "unknown"

    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        import time as _time
        from ragnarbot.cron.logger import log_execution

        start_time = _time.time()
        response = None
        status = "ok"
        error = None

        try:
            if job.payload.mode == "session":
                # Inject into user's active session via inbound queue
                cron_header = f"[Cron task: {job.name}]\n---\n{job.payload.message}"
                from ragnarbot.bus.events import InboundMessage
                await bus.publish_inbound(InboundMessage(
                    channel=job.payload.channel or "cli",
                    sender_id="cron",
                    chat_id=job.payload.to or "direct",
                    content=cron_header,
                    metadata={"cron_job_id": job.id},
                ))
                response = "(queued to session)"
            else:
                # Isolated mode — fully parallel, no locks
                schedule_desc = _format_schedule(job.schedule)
                response = await agent.process_cron_isolated(
                    job_name=job.name,
                    message=job.payload.message,
                    schedule_desc=schedule_desc,
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to or "direct",
                )
                # Deliver result to user
                if response and job.payload.to:
                    from ragnarbot.bus.events import OutboundMessage
                    await bus.publish_outbound(OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response,
                    ))
        except Exception as e:
            status = "error"
            error = str(e)
            raise
        finally:
            duration = _time.time() - start_time
            log_execution(job, response, status, duration, error)

            # Append a silent marker to the user's active session so the
            # main agent knows an isolated job ran (without triggering a turn).
            if job.payload.mode == "isolated" and job.payload.to:
                try:
                    channel = job.payload.channel or "cli"
                    session_key = f"{channel}:{job.payload.to}"
                    session = agent.sessions.get_or_create(session_key)
                    ts = _time.strftime("%Y-%m-%d %H:%M:%S")
                    marker = (
                        f"[Cron result: {job.name} | id: {job.id} "
                        f"| {ts} | status: {status}]"
                    )
                    session.add_message("assistant", marker)
                    agent.sessions.save(session)
                except Exception as marker_err:
                    from loguru import logger as _log
                    _log.warning(f"Failed to save cron marker: {marker_err}")

        return response
    cron.on_job = on_cron_job

    # Create heartbeat service
    import time as _time
    from ragnarbot.bus.events import InboundMessage as _InboundMessage

    async def on_heartbeat() -> tuple[str | None, str | None, str | None]:
        return await agent.process_heartbeat()

    async def on_heartbeat_deliver(result: str, channel: str, chat_id: str):
        """Phase 2: inject heartbeat result into user's active chat."""
        await bus.publish_inbound(_InboundMessage(
            channel=channel,
            sender_id="heartbeat",
            chat_id=chat_id,
            content=f"[Heartbeat report]\n---\n{result}",
            metadata={
                "heartbeat_result": True,
                "system_note": (
                    "[System] This is an internal message — the user does not see it. "
                    "Relay the results to the user naturally, in the tone and context "
                    "of your conversation. Do not mention the heartbeat mechanism."
                ),
            },
        ))

    async def on_heartbeat_complete(channel: str | None, chat_id: str | None):
        """Save silent marker to user's session."""
        if not channel or not chat_id:
            return
        session_key = f"{channel}:{chat_id}"
        session = agent.sessions.get_or_create(session_key)
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        marker = f"[Heartbeat check | {ts} | silent]"
        session.add_message("assistant", marker)
        agent.sessions.save(session)

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        on_deliver=on_heartbeat_deliver,
        on_complete=on_heartbeat_complete,
        interval_m=config.heartbeat.interval_m,
        enabled=config.heartbeat.enabled,
    )

    # Create channel manager
    channels = ChannelManager(config, bus, creds, media_manager=media_manager)

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    hb_status = f"every {config.heartbeat.interval_m}m" if config.heartbeat.enabled else "disabled"
    console.print(f"[green]✓[/green] Heartbeat: {hb_status}")

    pid_path = Path.home() / ".ragnarbot" / "gateway.pid"

    async def run():
        # Write PID file
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))

        # If this is a post-update restart, notify the originating channel
        from ragnarbot.agent.tools.update import UPDATE_MARKER, GITHUB_REPO
        if UPDATE_MARKER.exists():
            try:
                import json as _json
                marker = _json.loads(UPDATE_MARKER.read_text())
                origin_channel = marker["channel"]
                origin_chat_id = marker["chat_id"]
                old_ver = marker.get("old_version", "?")
                new_ver = marker.get("new_version", "?")
                changelog_url = (
                    f"https://github.com/{GITHUB_REPO}/compare/v{old_ver}...v{new_ver}"
                )
                from ragnarbot.bus.events import InboundMessage
                await bus.publish_inbound(InboundMessage(
                    channel="system",
                    sender_id="gateway",
                    chat_id=f"{origin_channel}:{origin_chat_id}",
                    content=(
                        f"[System: ragnarbot updated from v{old_ver} to v{new_ver}. "
                        f"Changelog: {changelog_url}]"
                    ),
                ))
                console.print(
                    f"[green]✓[/green] Post-update notification queued "
                    f"for {origin_channel}:{origin_chat_id} (v{old_ver} → v{new_ver})"
                )
            except Exception as e:
                console.print(
                    f"[yellow]Warning: could not inject update notification: {e}[/yellow]"
                )
            finally:
                UPDATE_MARKER.unlink(missing_ok=True)

        # If this is a restart, inject a notification into the originating channel
        from ragnarbot.agent.tools.restart import RESTART_MARKER
        if RESTART_MARKER.exists():
            try:
                import json as _json
                marker = _json.loads(RESTART_MARKER.read_text())
                origin_channel = marker["channel"]
                origin_chat_id = marker["chat_id"]
                from ragnarbot.bus.events import InboundMessage
                await bus.publish_inbound(InboundMessage(
                    channel="system",
                    sender_id="gateway",
                    chat_id=f"{origin_channel}:{origin_chat_id}",
                    content=(
                        "[System: gateway restarted successfully. "
                        "Config changes are now active.]"
                    ),
                ))
                console.print(
                    f"[green]✓[/green] Post-restart notification queued "
                    f"for {origin_channel}:{origin_chat_id}"
                )
            except Exception as e:
                console.print(f"[yellow]Warning: could not inject restart notification: {e}[/yellow]")
            finally:
                RESTART_MARKER.unlink(missing_ok=True)

        # SIGUSR1 handler for config reload
        reload_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        import signal
        try:
            loop.add_signal_handler(signal.SIGUSR1, reload_event.set)
        except NotImplementedError:
            pass  # Windows — signal handler not supported

        async def _config_reloader():
            while True:
                await reload_event.wait()
                reload_event.clear()
                try:
                    new_config = load_config()
                    for ch in channels.channels.values():
                        ch.config = getattr(new_config.channels, ch.name, ch.config)
                    console.print("[green]✓[/green] Config reloaded (SIGUSR1)")
                except Exception as e:
                    console.print(f"[red]Config reload failed: {e}[/red]")

        try:
            await cron.start()
            await heartbeat.start()

            agent_task = asyncio.create_task(agent.run())
            channel_task = asyncio.create_task(channels.start_all())
            reloader_task = asyncio.create_task(_config_reloader())

            # Wait for agent to finish (normal stop or restart request)
            await agent_task

            # Cancel other tasks
            for task in [channel_task, reloader_task]:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Cleanup
            heartbeat.stop()
            cron.stop()
            await channels.stop_all()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())

    if agent.restart_requested:
        pid_path.unlink(missing_ok=True)
        console.print("[green]✓[/green] Restarting gateway...")
        import sys
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        pid_path.unlink(missing_ok=True)


@gateway_app.command("start")
def gateway_start():
    """Install and start the gateway daemon."""
    from ragnarbot.daemon import DaemonError, DaemonStatus, get_manager
    from ragnarbot.daemon.resolve import UnsupportedPlatformError

    try:
        manager = get_manager()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    from ragnarbot.config.migration import run_startup_migration
    if not run_startup_migration(console):
        raise typer.Exit(0)

    try:
        info = manager.status()
        if info.status == DaemonStatus.RUNNING:
            console.print(f"[green]Gateway is already running[/green] (PID {info.pid})")
            return

        if not manager.is_installed():
            manager.install()
            console.print("[green]Daemon installed[/green]")

        manager.start()
        console.print("[green]Gateway started[/green]")
    except DaemonError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@gateway_app.command("stop")
def gateway_stop():
    """Stop the gateway daemon."""
    from ragnarbot.daemon import DaemonError, DaemonStatus, get_manager
    from ragnarbot.daemon.resolve import UnsupportedPlatformError

    try:
        manager = get_manager()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not manager.is_installed():
        console.print("[yellow]Daemon is not installed[/yellow]")
        raise typer.Exit(1)

    try:
        info = manager.status()
        if info.status != DaemonStatus.RUNNING:
            console.print("[yellow]Gateway is not running[/yellow]")
            return

        manager.stop()
        console.print("[green]Gateway stopped[/green]")
    except DaemonError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@gateway_app.command("restart")
def gateway_restart():
    """Restart the gateway daemon."""
    from ragnarbot.daemon import DaemonError, get_manager
    from ragnarbot.daemon.resolve import UnsupportedPlatformError

    try:
        manager = get_manager()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    from ragnarbot.config.migration import run_startup_migration
    if not run_startup_migration(console):
        raise typer.Exit(0)

    try:
        if not manager.is_installed():
            manager.install()
            console.print("[green]Daemon installed[/green]")

        manager.restart()
        console.print("[green]Gateway restarted[/green]")
    except DaemonError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@gateway_app.command("delete")
def gateway_delete():
    """Stop and remove the gateway daemon."""
    from ragnarbot.daemon import DaemonError, DaemonStatus, get_manager
    from ragnarbot.daemon.resolve import UnsupportedPlatformError

    try:
        manager = get_manager()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not manager.is_installed():
        console.print("[yellow]Daemon is not installed[/yellow]")
        raise typer.Exit(1)

    try:
        info = manager.status()
        if info.status == DaemonStatus.RUNNING:
            manager.stop()
            console.print("[green]Gateway stopped[/green]")

        manager.uninstall()
        console.print("[green]Daemon removed[/green]")
    except DaemonError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@gateway_app.command("status")
def gateway_status():
    """Show gateway daemon status."""
    from ragnarbot.daemon import DaemonStatus, get_manager
    from ragnarbot.daemon.resolve import UnsupportedPlatformError

    try:
        manager = get_manager()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    info = manager.status()

    status_styles = {
        DaemonStatus.RUNNING: "[green]running[/green]",
        DaemonStatus.STOPPED: "[yellow]stopped[/yellow]",
        DaemonStatus.NOT_INSTALLED: "[dim]not installed[/dim]",
    }

    console.print(f"Status:       {status_styles[info.status]}")
    if info.pid:
        console.print(f"PID:          {info.pid}")
    if info.service_file:
        console.print(f"Service file: {info.service_file}")
    if info.log_path:
        console.print(f"Logs:         {info.log_path}")



# ============================================================================
# Telegram Commands
# ============================================================================

telegram_app = typer.Typer(help="Telegram channel management")
app.add_typer(telegram_app, name="telegram")


@telegram_app.command("grant-access")
def telegram_grant_access(
    code: str = typer.Argument(..., help="Access code shown to the user"),
):
    """Grant bot access to a Telegram user via an access code."""
    from ragnarbot.auth.credentials import load_credentials
    from ragnarbot.auth.grants import PendingGrantStore
    from ragnarbot.config.loader import load_config, save_config

    store = PendingGrantStore()
    grant = store.validate(code)
    if not grant:
        console.print("[red]Error: Invalid or expired access code.[/red]")
        raise typer.Exit(1)

    config = load_config()
    creds = load_credentials()

    # Add user_id to allow_from if not already present
    allow_from = config.channels.telegram.allow_from
    if grant.user_id in allow_from:
        console.print(f"[yellow]User {grant.user_id} is already in the allow list.[/yellow]")
    else:
        allow_from.append(grant.user_id)
        save_config(config)
        console.print(f"[green]✓[/green] Added user {grant.user_id} to allow list.")

    # Remove used grant code
    store.remove(code)

    # Send confirmation to user via Telegram
    bot_token = creds.channels.telegram.bot_token
    if bot_token:
        import asyncio

        async def _send_confirmation():
            from telegram import Bot

            from ragnarbot.channels.telegram import set_bot_commands
            bot = Bot(token=bot_token)
            async with bot:
                await bot.send_message(
                    chat_id=int(grant.chat_id),
                    text=(
                        "<b>Access Granted</b>\n\n"
                        "This account has been successfully added to the bot."
                    ),
                    parse_mode="HTML",
                )
                await set_bot_commands(bot)

        try:
            asyncio.run(_send_confirmation())
        except Exception as e:
            console.print(f"[yellow]Warning: Could not send confirmation via Telegram: {e}[/yellow]")
    else:
        console.print("[yellow]No bot token configured — skipping Telegram notification.[/yellow]")

    # Signal running gateway to reload config
    _signal_gateway_reload()


def _signal_gateway_reload() -> None:
    """Try to signal the running gateway to reload its config."""
    import signal

    pid_path = Path.home() / ".ragnarbot" / "gateway.pid"
    if not pid_path.exists():
        console.print("[dim]Gateway not running (no PID file). Restart to apply changes.[/dim]")
        return

    try:
        pid = int(pid_path.read_text().strip())
        import os
        os.kill(pid, signal.SIGUSR1)
        console.print("[green]✓[/green] Signaled gateway to reload config.")
    except ProcessLookupError:
        console.print("[dim]Stale PID file. Restart gateway to apply changes.[/dim]")
    except (ValueError, OSError) as e:
        console.print(f"[dim]Could not signal gateway: {e}. Restart to apply changes.[/dim]")


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from ragnarbot.auth.credentials import load_credentials
    from ragnarbot.config.loader import load_config

    config = load_config()
    creds = load_credentials()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # Telegram
    tg = config.channels.telegram
    tg_token = creds.channels.telegram.bot_token
    tg_config = f"token: {tg_token[:10]}..." if tg_token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show ragnarbot status."""
    from ragnarbot.auth.credentials import get_credentials_path, load_credentials
    from ragnarbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    creds_path = get_credentials_path()
    config = load_config()
    creds = load_credentials()
    workspace = config.workspace_path

    console.print(f"{__logo__} ragnarbot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(
        f"Credentials: {creds_path} {'[green]✓[/green]' if creds_path.exists() else '[red]✗[/red]'}"
    )
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")

        auth_method = config.agents.defaults.auth_method
        provider_name = (
            config.agents.defaults.model.split("/")[0]
            if "/" in config.agents.defaults.model
            else "anthropic"
        )

        for name in ("anthropic", "openai", "gemini"):
            pc = getattr(creds.providers, name)
            if name == provider_name and auth_method == "oauth":
                if name == "gemini":
                    from ragnarbot.auth.gemini_oauth import is_authenticated as _gem_auth
                    auth_info = "[green]oauth[/green]" if _gem_auth() else "[dim]not set[/dim]"
                elif name == "openai":
                    from ragnarbot.auth.openai_oauth import is_authenticated as _oai_auth
                    auth_info = "[green]oauth[/green]" if _oai_auth() else "[dim]not set[/dim]"
                elif pc.oauth_key:
                    auth_info = "[green]oauth[/green]"
                else:
                    auth_info = "[dim]not set[/dim]"
            elif pc.api_key:
                auth_info = "[green]api_key[/green]"
            else:
                auth_info = "[dim]not set[/dim]"
            console.print(f"{name.capitalize()}: {auth_info}")


if __name__ == "__main__":
    app()
