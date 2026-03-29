"""CLI commands for ragnarbot."""

import asyncio
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ragnarbot import __logo__, __version__
from ragnarbot.instance import (
    GatewayClaimError,
    acquire_gateway_claim,
    clear_pending_update,
    get_instance,
    get_live_gateway_pid,
    load_pending_update,
    record_process_start,
    release_gateway_claim,
    runtime_name,
    set_active_profile,
    signal_live_gateway,
    tilde_path,
)
from ragnarbot.providers.lightning import LIGHTNING_UNSUPPORTED_NOTE, resolve_lightning

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

    if provider_name == "anthropic":
        api_key = provider_creds.api_key if provider_creds else None
        from ragnarbot.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=api_key, default_model=model)

    api_key = provider_creds.api_key if provider_creds else None
    return LiteLLMProvider(api_key=api_key, default_model=model)


def _validate_auth(config, creds):
    """Validate auth configuration before provider creation.

    Validates both the primary model and fallback model (if configured).
    Returns error message string or None if OK.
    """
    from ragnarbot.config.validation import validate_model_auth

    auth_method = config.agents.defaults.auth_method
    if auth_method not in ("api_key", "oauth"):
        return f"Unknown auth method: {auth_method}"

    # Validate primary model
    error = validate_model_auth(config.agents.defaults.model, auth_method, creds)
    if error:
        return error

    # Validate fallback model (if configured)
    fb = config.agents.fallback
    if fb.model:
        fb_error = validate_model_auth(fb.model, fb.auth_method, creds)
        if fb_error:
            return f"Fallback model: {fb_error}"

    return None


def _running_gateway_pid() -> int | None:
    """Return the live PID for the active profile, if any."""
    return get_live_gateway_pid()


def _acquire_gateway_claim_or_exit(instance=None) -> None:
    """Acquire the profile-local gateway claim or exit with a user-facing error."""
    instance = instance or get_instance()
    try:
        acquire_gateway_claim()
    except GatewayClaimError as e:
        pid = e.pid
        console.print(
            f"[red]Error:[/red] {instance.runtime_name} is already running "
            f"(PID {pid if pid is not None else '?'}, profile '{instance.profile}')."
        )
        raise typer.Exit(1)


def _pending_update_mode(payload: dict | None) -> str | None:
    """Return the display mode for a pending update payload."""
    if not payload:
        return None
    return "pending restart" if payload.get("requires_restart", True) else "pending notice"


def _print_pending_update(prefix: str = "Update") -> None:
    """Print the current profile's pending-update state if present."""
    pending = load_pending_update()
    if not pending:
        return
    mode = _pending_update_mode(pending) or "pending"
    console.print(
        f"{prefix}:      [yellow]{mode}[/yellow] "
        f"{pending.get('old_version', '?')} -> {pending.get('new_version', '?')}"
    )


def _reconcile_pending_update_after_startup() -> dict | None:
    """Clear restart-required pending updates once the new version is running."""
    pending = load_pending_update()
    if (
        pending
        and pending.get("requires_restart", True)
        and pending.get("new_version") == __version__
    ):
        clear_pending_update()
        return None
    return pending


@app.callback()
def main(
    profile: str | None = typer.Option(
        None, "--profile", help="Run against a specific profile root",
    ),
    version: bool = typer.Option(
        None, "--version", "-v", is_eager=True,
    ),
):
    """ragnarbot - Personal AI Assistant."""
    try:
        set_active_profile(profile)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2)

    if version:
        console.print(f"{__logo__} {runtime_name()} v{__version__}")
        raise typer.Exit()


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
    from ragnarbot.hooks.service import HookService
    from ragnarbot.hooks.types import HookDefinition
    from ragnarbot.media.manager import MediaManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    instance = get_instance()
    _acquire_gateway_claim_or_exit(instance)
    record_process_start(os.getpid(), __version__)

    agent = None
    restart_requested = False

    try:
        console.print(f"{__logo__} Starting {instance.runtime_name} gateway on port {port}...")

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

        fallback_config = config.agents.fallback
        brave_api_key = creds.services.brave_search.api_key or None
        search_engine = config.tools.web.search.engine

        media_dir = get_data_dir() / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_manager = MediaManager(base_dir=media_dir)

        cron_store_path = get_data_dir() / "cron" / "jobs.json"
        cron = CronService(cron_store_path)

        hooks_store_path = get_data_dir() / "hooks" / "hooks.json"
        hooks_logs_dir = get_data_dir() / "hooks" / "logs"
        hook_service = HookService(hooks_store_path, hooks_logs_dir)

        agent = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            brave_api_key=brave_api_key,
            search_engine=search_engine,
            exec_config=config.tools.exec,
            cron_service=cron,
            hook_service=hook_service,
            stream_steps=config.agents.defaults.stream_steps,
            media_manager=media_manager,
            debounce_seconds=config.agents.defaults.debounce_seconds,
            max_context_tokens=config.agents.defaults.max_context_tokens,
            context_mode=config.agents.defaults.context_mode,
            reasoning_level=config.agents.defaults.reasoning_level,
            lightning_mode=config.agents.defaults.lightning_mode,
            auth_method=config.agents.defaults.auth_method,
            trace_mode=config.agents.defaults.trace_mode,
            steering_enabled=config.agents.defaults.steering_enabled,
            experimental_soul=config.agents.defaults.experimental_soul,
            heartbeat_interval_m=config.heartbeat.interval_m,
            fallback_model=fallback_config.model,
            fallback_config=fallback_config,
            provider_factory=lambda model, auth_method: _create_provider(
                model, auth_method, creds,
            ),
            browser_config=config.tools.browser,
        )

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
                    schedule_desc = _format_schedule(job.schedule)
                    response = await agent.process_cron_isolated(
                        job_name=job.name,
                        message=job.payload.message,
                        schedule_desc=schedule_desc,
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to or "direct",
                        agent_name=job.payload.agent,
                    )
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

        async def on_hook_trigger(hook: HookDefinition, payload: str) -> str | None:
            """Execute a hook trigger through the agent."""
            import time as _time

            start_time = _time.time()
            response = None
            status = "ok"
            error = None

            try:
                response = await agent.process_hook_isolated(
                    hook_name=hook.name,
                    instructions=hook.instructions,
                    payload=payload,
                    mode=hook.mode,
                    channel=hook.channel or "cli",
                    chat_id=hook.to or "direct",
                )
                if response and hook.to:
                    from ragnarbot.bus.events import OutboundMessage
                    await bus.publish_outbound(OutboundMessage(
                        channel=hook.channel or "cli",
                        chat_id=hook.to,
                        content=response,
                    ))
            except Exception as e:
                status = "error"
                error = str(e)
                raise
            finally:
                duration = _time.time() - start_time
                hook_service.log_trigger(hook, payload, status, duration, response, error)

                if hook.to:
                    try:
                        channel = hook.channel or "cli"
                        session_key = f"{channel}:{hook.to}"
                        session = agent.sessions.get_or_create(session_key)
                        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
                        marker = (
                            f"[Hook triggered: {hook.name} | id: {hook.id[:16]}... "
                            f"| {ts} | status: {status}]"
                        )
                        session.add_message("assistant", marker)
                        agent.sessions.save(session)
                    except Exception as marker_err:
                        from loguru import logger as _log
                        _log.warning(f"Failed to save hook marker: {marker_err}")

            return response

        hook_service.on_trigger = on_hook_trigger

        # Create hook HTTP server if enabled
        hook_server = None
        if config.hooks.enabled:
            from ragnarbot.hooks.server import HookServer
            hook_server = HookServer(
                service=hook_service,
                host=config.gateway.host,
                port=config.hooks.port,
                max_payload_bytes=config.hooks.max_payload_bytes,
                rate_limit_per_hook=config.hooks.rate_limit_per_hook,
            )

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

        hooks_count = len(hook_service.list_hooks(include_disabled=True))
        if config.hooks.enabled:
            console.print(
                f"[green]✓[/green] Hooks: port {config.hooks.port}, "
                f"{hooks_count} registered"
            )
        elif hooks_count > 0:
            console.print(
                f"[yellow]![/yellow] Hooks: disabled ({hooks_count} registered, "
                f"enable with hooks.enabled=true)"
            )

        async def run():
            _reconcile_pending_update_after_startup()

            # If this is a post-update restart, notify the originating channel
            from ragnarbot.agent.tools.update import GITHUB_REPO, get_update_marker_path
            update_marker = get_update_marker_path()
            if update_marker.exists():
                try:
                    import json as _json
                    marker = _json.loads(update_marker.read_text())
                    origin_channel = marker["channel"]
                    origin_chat_id = marker["chat_id"]
                    old_ver = marker.get("old_version", "?")
                    new_ver = marker.get("new_version", "?")
                    changelog_url = marker.get("changelog_url") or (
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
                    update_marker.unlink(missing_ok=True)

            # If this is a restart, inject a notification into the originating channel
            from ragnarbot.agent.tools.restart import get_restart_marker_path
            restart_marker = get_restart_marker_path()
            if restart_marker.exists():
                try:
                    import json as _json
                    marker = _json.loads(restart_marker.read_text())
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
                    restart_marker.unlink(missing_ok=True)

            # SIGUSR1 handler for config reload, SIGUSR2 for pending update notices.
            reload_event = asyncio.Event()
            update_notice_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            import signal
            try:
                loop.add_signal_handler(signal.SIGUSR1, reload_event.set)
                loop.add_signal_handler(signal.SIGUSR2, update_notice_event.set)
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

            async def _pending_update_notifier():
                while True:
                    await update_notice_event.wait()
                    update_notice_event.clear()
                    try:
                        delivered = await agent.queue_pending_update_notice()
                        if delivered:
                            console.print("[green]✓[/green] Pending update notice queued.")
                    except Exception as e:
                        console.print(f"[yellow]Warning: pending update notice failed: {e}[/yellow]")

            try:
                await cron.start()
                await heartbeat.start()
                if hook_server:
                    await hook_server.start()

                agent_task = asyncio.create_task(agent.run())
                channel_task = asyncio.create_task(channels.start_all())
                reloader_task = asyncio.create_task(_config_reloader())
                notice_task = asyncio.create_task(_pending_update_notifier())

                # Wait for agent to finish (normal stop or restart request)
                await agent_task

                # Cancel other tasks
                for task in [channel_task, reloader_task, notice_task]:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Cleanup
                if hook_server:
                    await hook_server.stop()
                await agent.browser_manager.close_all()
                heartbeat.stop()
                cron.stop()
                await channels.stop_all()
            except KeyboardInterrupt:
                console.print("\nShutting down...")
                if hook_server:
                    await hook_server.stop()
                await agent.browser_manager.close_all()
                heartbeat.stop()
                cron.stop()
                agent.stop()
                await channels.stop_all()

        asyncio.run(run())

        restart_requested = bool(agent and agent.restart_requested)
        if restart_requested:
            console.print("[green]✓[/green] Restarting gateway...")
            import sys
            os.execv(sys.executable, [sys.executable] + sys.argv)
    finally:
        if not restart_requested:
            release_gateway_claim()


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
        pid = _running_gateway_pid()
        if pid:
            console.print(f"[green]Gateway is already running[/green] (PID {pid})")
            return

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

    instance = get_instance()
    runtime_pid = _running_gateway_pid()

    console.print(f"Instance:     {instance.runtime_name}")
    console.print(f"Profile:      {instance.profile}")
    console.print(f"Data root:    {tilde_path(instance.data_root)}")
    console.print(f"Status:       {status_styles[info.status]}")
    if info.pid:
        console.print(f"PID:          {info.pid}")
    elif runtime_pid:
        console.print(f"PID:          {runtime_pid} [dim](foreground)[/dim]")
    if info.service_file:
        console.print(f"Service file: {info.service_file}")
    if info.log_path:
        console.print(f"Logs:         {info.log_path}")
    _print_pending_update()



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

    pid = signal_live_gateway(signal.SIGUSR1)
    if pid is not None:
        console.print("[green]✓[/green] Signaled gateway to reload config.")
        return
    console.print("[dim]Gateway not running or claim is stale. Restart to apply changes.[/dim]")


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

    instance = get_instance()
    config_path = get_config_path()
    creds_path = get_credentials_path()
    config = load_config()
    creds = load_credentials()
    workspace = config.workspace_path

    console.print(f"{__logo__} {instance.runtime_name} Status\n")
    console.print(f"Profile: {instance.profile}")
    console.print(f"Data root: {tilde_path(instance.data_root)}")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(
        f"Credentials: {creds_path} {'[green]✓[/green]' if creds_path.exists() else '[red]✗[/red]'}"
    )
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")
    _print_pending_update()

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")
        lightning_resolution = resolve_lightning(
            config.agents.defaults.model,
            config.agents.defaults.auth_method,
            config.agents.defaults.lightning_mode,
        )
        lightning_status = "Enabled" if config.agents.defaults.lightning_mode else "Disabled"
        if config.agents.defaults.lightning_mode and not lightning_resolution.supported:
            lightning_status = f"{lightning_status} [yellow](no effect for current model/auth)[/yellow]"
        console.print(f"Lightning: {lightning_status}")
        if config.agents.defaults.lightning_mode and not lightning_resolution.supported:
            console.print(f"[yellow]{LIGHTNING_UNSUPPORTED_NOTE}[/yellow]")

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
