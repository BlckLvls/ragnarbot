"""CLI commands for ragnarbot."""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ragnarbot import __version__, __logo__

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

    provider_creds = getattr(creds.providers, provider_name, None)
    oauth_token = None
    api_key = None

    if provider_creds:
        if provider_creds.auth_method == "oauth" and provider_creds.oauth.access_token:
            oauth_token = provider_creds.oauth.access_token
        elif provider_creds.api_key:
            api_key = provider_creds.api_key

    return api_key, oauth_token, provider_name


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
    """Initialize ragnarbot configuration and workspace."""
    from ragnarbot.config.loader import get_config_path, save_config
    from ragnarbot.config.schema import Config
    from ragnarbot.utils.helpers import get_workspace_path

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Create default bootstrap files
    _create_workspace_templates(workspace)

    console.print(f"\n{__logo__} ragnarbot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.ragnarbot/credentials.json[/cyan]")
    console.print("     Get one at: https://console.anthropic.com/keys")
    console.print("  2. Chat: [cyan]ragnarbot agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram? See: https://github.com/BlckLvls/ragnarbot#-chat-apps[/dim]")




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


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the ragnarbot gateway."""
    from ragnarbot.config.loader import load_config, get_data_dir
    from ragnarbot.auth.credentials import load_credentials
    from ragnarbot.bus.queue import MessageBus
    from ragnarbot.providers.litellm_provider import LiteLLMProvider
    from ragnarbot.agent.loop import AgentLoop
    from ragnarbot.channels.manager import ChannelManager
    from ragnarbot.cron.service import CronService
    from ragnarbot.cron.types import CronJob
    from ragnarbot.heartbeat.service import HeartbeatService

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting ragnarbot gateway on port {port}...")

    config = load_config()
    creds = load_credentials()

    # Create components
    bus = MessageBus()

    # Resolve provider auth from credentials
    api_key, oauth_token, provider_name = _resolve_provider_auth(config, creds)
    api_base = config.get_api_base()

    if not api_key and not oauth_token:
        console.print("[red]Error: No API key or OAuth token configured.[/red]")
        console.print("Set credentials in ~/.ragnarbot/credentials.json")
        raise typer.Exit(1)

    if provider_name == "anthropic" and oauth_token:
        from ragnarbot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            oauth_token=oauth_token,
            api_base=api_base,
            default_model=config.agents.defaults.model,
        )
    else:
        provider = LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=config.agents.defaults.model,
            oauth_token=oauth_token,
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

    console.print(f"[green]✓[/green] Heartbeat: every 30m")

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




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from ragnarbot.config.loader import load_config
    from ragnarbot.auth.credentials import load_credentials
    from ragnarbot.bus.queue import MessageBus
    from ragnarbot.providers.litellm_provider import LiteLLMProvider
    from ragnarbot.agent.loop import AgentLoop

    config = load_config()
    creds = load_credentials()

    api_key, oauth_token, provider_name = _resolve_provider_auth(config, creds)
    api_base = config.get_api_base()

    if not api_key and not oauth_token:
        console.print("[red]Error: No API key or OAuth token configured.[/red]")
        console.print("Set credentials in ~/.ragnarbot/credentials.json")
        raise typer.Exit(1)

    bus = MessageBus()

    if provider_name == "anthropic" and oauth_token:
        from ragnarbot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            oauth_token=oauth_token,
            api_base=api_base,
            default_model=config.agents.defaults.model,
        )
    else:
        provider = LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=config.agents.defaults.model,
            oauth_token=oauth_token,
        )

    brave_api_key = creds.services.web_search.api_key or None

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        brave_api_key=brave_api_key,
        exec_config=config.tools.exec,
    )

    if message:
        # Single message mode
        async def run_once():
            response = await agent_loop.process_direct(message, session_id)
            console.print(f"\n{__logo__} {response}")

        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await agent_loop.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from ragnarbot.config.loader import load_config
    from ragnarbot.auth.credentials import load_credentials

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
        console.print(f"[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show ragnarbot status."""
    from ragnarbot.config.loader import load_config, get_config_path
    from ragnarbot.auth.credentials import load_credentials, get_credentials_path

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

        # Show auth status per provider from credentials
        for name in ("anthropic", "openai", "gemini"):
            pc = getattr(creds.providers, name)
            if pc.auth_method == "oauth" and pc.oauth.access_token:
                auth_info = "[green]oauth[/green]"
            elif pc.api_key:
                auth_info = "[green]api_key[/green]"
            else:
                auth_info = "[dim]not set[/dim]"
            console.print(f"{name.capitalize()}: {auth_info}")


if __name__ == "__main__":
    app()
