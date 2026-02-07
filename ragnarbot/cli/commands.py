"""CLI commands for ragnarbot."""

import asyncio
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


def _resolve_provider_auth(config, creds):
    """Resolve API key and OAuth token from credentials for the active provider.

    Returns (api_key, oauth_token, provider_name).
    """
    model = config.agents.defaults.model
    provider_name = model.split("/")[0] if "/" in model else "anthropic"
    auth_method = config.agents.defaults.auth_method

    provider_creds = getattr(creds.providers, provider_name, None)
    oauth_token = None
    api_key = None

    if provider_creds:
        if auth_method == "oauth" and provider_creds.oauth_key:
            oauth_token = provider_creds.oauth_key
        elif provider_creds.api_key:
            api_key = provider_creds.api_key

    return api_key, oauth_token, provider_name


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

    provider_creds = getattr(creds.providers, provider_name, None)
    if not provider_creds:
        return f"No credentials configured for provider '{provider_name}'"

    if auth_method == "api_key" and not provider_creds.api_key:
        return f"No API key configured for '{provider_name}'. Set it in ~/.ragnarbot/credentials.json"

    if auth_method == "oauth" and not provider_creds.oauth_key:
        return f"No OAuth token for '{provider_name}'. Run: claude setup-token"

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




def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
        "SOUL.md": """# Soul

I am ragnarbot, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
    }

    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")

    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


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
    from ragnarbot.providers.litellm_provider import LiteLLMProvider

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting ragnarbot gateway on port {port}...")

    config = load_config()
    creds = load_credentials()

    # Create components
    bus = MessageBus()

    # Validate auth configuration
    error = _validate_auth(config, creds)
    if error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(1)

    api_key, oauth_token, provider_name = _resolve_provider_auth(config, creds)

    if provider_name == "anthropic" and oauth_token:
        from ragnarbot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            oauth_token=oauth_token,
            default_model=config.agents.defaults.model,
        )
    else:
        provider = LiteLLMProvider(
            api_key=api_key,
            default_model=config.agents.defaults.model,
        )

    # Service credentials
    brave_api_key = creds.services.web_search.api_key or None

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=brave_api_key,
        exec_config=config.tools.exec,
        cron_service=cron,
        stream_steps=config.agents.defaults.stream_steps,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            from ragnarbot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(prompt, session_key="heartbeat")

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )

    # Create channel manager
    channels = ChannelManager(config, bus, creds)

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


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
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from ragnarbot.config.loader import get_data_dir
    from ragnarbot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram')"),
):
    """Add a scheduled job."""
    from ragnarbot.config.loader import get_data_dir
    from ragnarbot.cron.service import CronService
    from ragnarbot.cron.types import CronSchedule

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from ragnarbot.config.loader import get_data_dir
    from ragnarbot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from ragnarbot.config.loader import get_data_dir
    from ragnarbot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from ragnarbot.config.loader import get_data_dir
    from ragnarbot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


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
            if name == provider_name and auth_method == "oauth" and pc.oauth_key:
                auth_info = "[green]oauth[/green]"
            elif pc.api_key:
                auth_info = "[green]api_key[/green]"
            else:
                auth_info = "[dim]not set[/dim]"
            console.print(f"{name.capitalize()}: {auth_info}")


if __name__ == "__main__":
    app()
