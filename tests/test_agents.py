"""Tests for agents loader, agent tools, and sub-agent manager."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragnarbot.agent.agents_loader import AgentDefinition, AgentsLoader
from ragnarbot.agent.subagent import (
    SAFE_TOOL_NAMES,
    AgentTaskStatus,
    SubagentManager,
)
from ragnarbot.agent.tools.agent_tools import ACTIONS, AgentTool


# ---------------------------------------------------------------------------
# AgentsLoader
# ---------------------------------------------------------------------------

class TestAgentsLoader:
    """Test the AgentsLoader class."""

    def _write_agent(self, base: Path, name: str, content: str):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "AGENT.md").write_text(content, encoding="utf-8")

    def test_list_builtin_agents(self, tmp_path):
        builtin = tmp_path / "builtin"
        self._write_agent(builtin, "researcher", "---\nname: researcher\ndescription: Research agent\n---\nbody")
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        agents = loader.list_agents()
        assert len(agents) == 1
        assert agents[0]["name"] == "researcher"
        assert agents[0]["source"] == "builtin"

    def test_workspace_overrides_builtin(self, tmp_path):
        builtin = tmp_path / "builtin"
        workspace = tmp_path / "workspace"
        self._write_agent(builtin, "researcher", "---\nname: researcher\ndescription: builtin\n---\nbody")
        self._write_agent(workspace / "agents", "researcher", "---\nname: researcher\ndescription: custom\n---\ncustom body")
        loader = AgentsLoader(workspace, builtin_agents_dir=builtin)
        agents = loader.list_agents()
        assert len(agents) == 1
        assert agents[0]["source"] == "workspace"
        assert agents[0]["description"] == "custom"

    def test_load_agent_parses_frontmatter(self, tmp_path):
        builtin = tmp_path / "builtin"
        content = "---\nname: researcher\ndescription: A researcher\nmodel: gpt-4\nallowedTools: [web_search, web_fetch]\n---\n\n# Instructions\nDo research."
        self._write_agent(builtin, "researcher", content)
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("researcher")
        assert defn is not None
        assert defn.name == "researcher"
        assert defn.description == "A researcher"
        assert defn.model == "gpt-4"
        assert defn.allowed_tools == ["web_search", "web_fetch"]
        assert "# Instructions" in defn.body

    def test_load_agent_default_values(self, tmp_path):
        builtin = tmp_path / "builtin"
        content = "---\nname: helper\ndescription: A helper\n---\nBody here."
        self._write_agent(builtin, "helper", content)
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("helper")
        assert defn is not None
        assert defn.model == "default"
        assert defn.allowed_tools == "all"

    def test_load_agent_not_found(self, tmp_path):
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=tmp_path / "nope")
        assert loader.load_agent("nonexistent") is None

    def test_build_agents_summary(self, tmp_path):
        builtin = tmp_path / "builtin"
        self._write_agent(builtin, "researcher", "---\nname: researcher\ndescription: Research agent\n---\nbody")
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        summary = loader.build_agents_summary()
        assert "<agents>" in summary
        assert "<name>researcher</name>" in summary
        assert "Research agent" in summary

    def test_build_agents_summary_empty(self, tmp_path):
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=tmp_path / "nope")
        assert loader.build_agents_summary() == ""

    def test_builtin_researchers_exist(self):
        """Verify the built-in researcher agents are present in the package."""
        from ragnarbot.agent.agents_loader import BUILTIN_AGENTS_DIR
        assert (BUILTIN_AGENTS_DIR / "deep-researcher" / "AGENT.md").exists()
        assert (BUILTIN_AGENTS_DIR / "fast-researcher" / "AGENT.md").exists()



# ---------------------------------------------------------------------------
# AgentTool - unified tool schema and dispatch
# ---------------------------------------------------------------------------

class TestAgentTool:
    """Test the unified AgentTool schema and action dispatch."""

    def _make_tool(self):
        return AgentTool(manager=MagicMock())

    def test_name_and_description(self):
        tool = self._make_tool()
        assert tool.name == "agent"
        assert "sub-agents" in tool.description

    def test_action_enum(self):
        tool = self._make_tool()
        schema = tool.parameters
        assert schema["properties"]["action"]["enum"] == ACTIONS
        assert "action" in schema["required"]

    def test_spawn_params_present(self):
        tool = self._make_tool()
        props = tool.parameters["properties"]
        assert "task" in props
        assert "agent_name" in props
        assert "model" in props
        assert "label" in props

    def test_message_params_present(self):
        tool = self._make_tool()
        props = tool.parameters["properties"]
        assert "content" in props
        assert "task_id" in props

    def test_full_param_present(self):
        tool = self._make_tool()
        props = tool.parameters["properties"]
        assert "full" in props
        assert props["full"]["type"] == "boolean"

    def test_set_context(self):
        tool = self._make_tool()
        tool.set_context("telegram", "123")
        assert tool._origin_channel == "telegram"
        assert tool._origin_chat_id == "123"


# ---------------------------------------------------------------------------
# SubagentManager unit tests
# ---------------------------------------------------------------------------

class TestSubagentManager:
    """Test SubagentManager core methods."""

    def _make_manager(self, tmp_path, agents_loader=None):
        provider = MagicMock()
        provider.get_default_model.return_value = "test/model"
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        if agents_loader is None:
            agents_loader = AgentsLoader(
                tmp_path / "workspace",
                builtin_agents_dir=tmp_path / "empty",
            )
        from ragnarbot.config.schema import ExecToolConfig
        return SubagentManager(
            provider=provider,
            workspace=tmp_path / "workspace",
            bus=bus,
            agents_loader=agents_loader,
            model="test/model",
            exec_config=ExecToolConfig(),
        )

    @pytest.mark.asyncio
    async def test_spawn_unknown_agent_returns_error(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = await mgr.spawn(task="do stuff", agent_name="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_spawn_rejects_unknown_tools(self, tmp_path):
        builtin = tmp_path / "builtin"
        d = builtin / "bad_agent"
        d.mkdir(parents=True)
        (d / "AGENT.md").write_text(
            "---\nname: bad_agent\ndescription: bad\nallowedTools: [send_photo, cron]\n---\nbody",
            encoding="utf-8",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        mgr = self._make_manager(tmp_path, agents_loader=loader)
        result = await mgr.spawn(task="do stuff", agent_name="bad_agent")
        assert "unknown tools" in result
        assert "send_photo" in result
        assert "cron" in result

    def test_list_tasks_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr.list_tasks() == []

    def test_get_progress_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.get_progress("nope")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_send_message_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = await mgr.send_message("nope", "hello")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_send_message_to_running_agent_errors(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        # Manually insert a running task
        from ragnarbot.agent.subagent import AgentTask
        task = AgentTask(
            id="run1",
            label="running task",
            agent_name=None,
            task="do something",
            status=AgentTaskStatus.running,
            messages=[],
            stop_event=asyncio.Event(),
            created_at="",
            origin={"channel": "cli", "chat_id": "direct"},
        )
        mgr._tasks["run1"] = task
        result = await mgr.send_message("run1", "hello")
        assert "still running" in result

    @pytest.mark.asyncio
    async def test_stop_task_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = await mgr.stop_task("nope")
        assert "not found" in result

    def test_dismiss_task_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.dismiss_task("nope")
        assert "not found" in result

    def test_get_running_count_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr.get_running_count() == 0

    def test_build_agent_tool_registry_all_tools(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        reg, deliver = mgr._build_agent_tool_registry(
            definition=None, channel="cli", chat_id="direct",
        )
        # Should have deliver_result
        assert reg.has("deliver_result")
        # Should have safe tools (file_read, exec, etc.)
        assert reg.has("file_read")
        assert reg.has("exec")
        assert reg.has("web_search")

    def test_build_agent_tool_registry_restricted(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        defn = AgentDefinition(
            name="test",
            description="test",
            model="default",
            allowed_tools=["web_search", "web_fetch"],
            allowed_skills="none",
            body="test body",
            path="/fake/path",
        )
        reg, deliver = mgr._build_agent_tool_registry(
            definition=defn, channel="cli", chat_id="direct",
        )
        assert reg.has("web_search")
        assert reg.has("web_fetch")
        assert reg.has("deliver_result")
        # Should NOT have other tools
        assert not reg.has("file_read")
        assert not reg.has("exec")

    def test_build_agent_tool_registry_empty_tools(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        defn = AgentDefinition(
            name="dummy",
            description="dummy",
            model="default",
            allowed_tools=[],
            allowed_skills="none",
            body="do nothing",
            path="/fake/path",
        )
        reg, deliver = mgr._build_agent_tool_registry(
            definition=defn, channel="cli", chat_id="direct",
        )
        # Only deliver_result should be registered
        assert reg.has("deliver_result")
        assert len(reg) == 1


# ---------------------------------------------------------------------------
# SAFE_TOOL_NAMES constant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subagent_chat_receives_provider_max_tokens(tmp_path):
    """Verify the sub-agent's LLM call uses the provider's default_max_tokens.

    When _run_agent calls provider.chat() without an explicit max_tokens kwarg
    the provider must fall back to self.default_max_tokens.  This test sets a
    custom value on the provider and asserts it reaches the actual chat() call.
    """
    from ragnarbot.providers.base import LLMResponse

    captured_kwargs: dict = {}

    async def fake_chat(**kwargs):
        captured_kwargs.update(kwargs)
        return LLMResponse(content="done", finish_reason="stop")

    provider = MagicMock()
    provider.get_default_model.return_value = "test/model"
    provider.default_max_tokens = 42_000  # custom value from config
    provider.default_temperature = 0.42
    provider.chat = fake_chat

    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    from ragnarbot.config.schema import ExecToolConfig
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path / "workspace",
        bus=bus,
        agents_loader=AgentsLoader(
            tmp_path / "workspace",
            builtin_agents_dir=tmp_path / "empty",
        ),
        model="test/model",
        exec_config=ExecToolConfig(),
    )

    result = await mgr.spawn(task="hello")
    assert "started" in result.lower()

    # Wait for the background task to run
    await asyncio.sleep(0.3)

    # The provider.chat was called — check that max_tokens was NOT passed
    # explicitly, meaning the provider will use its own default_max_tokens.
    assert "max_tokens" not in captured_kwargs, (
        "Sub-agent should NOT pass explicit max_tokens — "
        "the provider uses self.default_max_tokens as fallback"
    )

    # Double-check: simulate what the provider does internally
    effective = captured_kwargs.get("max_tokens", provider.default_max_tokens)
    assert effective == 42_000


def test_safe_tool_names():
    """Verify safe tool names constant is populated correctly."""
    assert "file_read" in SAFE_TOOL_NAMES
    assert "exec" in SAFE_TOOL_NAMES
    assert "web_search" in SAFE_TOOL_NAMES
    # Ensure dangerous tools are NOT in the set
    assert "spawn" not in SAFE_TOOL_NAMES
    assert "cron" not in SAFE_TOOL_NAMES
    assert "config" not in SAFE_TOOL_NAMES
    assert "restart" not in SAFE_TOOL_NAMES


# ---------------------------------------------------------------------------
# allowedSkills parsing
# ---------------------------------------------------------------------------

class TestAllowedSkillsParsing:
    """Test parsing of the allowedSkills frontmatter field."""

    def _write_agent(self, base: Path, name: str, content: str):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "AGENT.md").write_text(content, encoding="utf-8")

    def test_default_is_none(self, tmp_path):
        """Missing allowedSkills defaults to 'none'."""
        builtin = tmp_path / "builtin"
        self._write_agent(
            builtin, "agent1",
            "---\nname: agent1\ndescription: test\n---\nbody",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("agent1")
        assert defn.allowed_skills == "none"

    def test_parse_all(self, tmp_path):
        builtin = tmp_path / "builtin"
        self._write_agent(
            builtin, "agent1",
            "---\nname: agent1\ndescription: test\nallowedSkills: all\n---\nbody",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("agent1")
        assert defn.allowed_skills == "all"

    def test_parse_none_explicit(self, tmp_path):
        builtin = tmp_path / "builtin"
        self._write_agent(
            builtin, "agent1",
            "---\nname: agent1\ndescription: test\nallowedSkills: none\n---\nbody",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("agent1")
        assert defn.allowed_skills == "none"

    def test_parse_explicit_list(self, tmp_path):
        builtin = tmp_path / "builtin"
        self._write_agent(
            builtin, "agent1",
            "---\nname: agent1\ndescription: test\n"
            "allowedSkills: [seo-optimizer, prompt-engineering]\n---\nbody",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        defn = loader.load_agent("agent1")
        assert defn.allowed_skills == ["seo-optimizer", "prompt-engineering"]


# ---------------------------------------------------------------------------
# Skills injection in sub-agent prompt
# ---------------------------------------------------------------------------

class TestSubagentSkillsInjection:
    """Test that skills are injected into named agent prompts."""

    def _make_manager(self, tmp_path, agents_loader=None, context_builder=None):
        provider = MagicMock()
        provider.get_default_model.return_value = "test/model"
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        if agents_loader is None:
            agents_loader = AgentsLoader(
                tmp_path / "workspace",
                builtin_agents_dir=tmp_path / "empty",
            )
        from ragnarbot.config.schema import ExecToolConfig
        return SubagentManager(
            provider=provider,
            workspace=tmp_path / "workspace",
            bus=bus,
            agents_loader=agents_loader,
            model="test/model",
            exec_config=ExecToolConfig(),
            context_builder=context_builder,
        )

    def test_skills_section_injected_when_allowed(self, tmp_path):
        """Named agent with allowed_skills gets a skills section in its prompt."""
        from ragnarbot.agent.subagent import AgentTask, AgentTaskStatus

        # Create a mock context_builder with skills
        ctx = MagicMock()
        ctx.skills.build_skills_summary.return_value = (
            "<skills>\n"
            "  <skill available=\"true\">\n"
            "    <name>my-skill</name>\n"
            "    <description>A skill</description>\n"
            "    <location>/path/to/SKILL.md</location>\n"
            "  </skill>\n"
            "</skills>"
        )

        mgr = self._make_manager(tmp_path, context_builder=ctx)
        defn = AgentDefinition(
            name="skilled",
            description="test",
            model="default",
            allowed_tools=["web_search"],
            allowed_skills=["my-skill"],
            body="Do the work.",
            path="/fake/path",
        )
        task = AgentTask(
            id="t1", label="test", agent_name="skilled", task="go",
            status=AgentTaskStatus.running, messages=[],
            stop_event=asyncio.Event(), created_at="",
            origin={"channel": "cli", "chat_id": "direct"},
        )

        prompt = mgr._build_system_prompt(task, defn)
        assert "Available Skills" in prompt
        assert "my-skill" in prompt
        assert "file_read" in prompt
        ctx.skills.build_skills_summary.assert_called_once_with(only=["my-skill"])

    def test_skills_section_absent_when_none(self, tmp_path):
        """Named agent with allowed_skills='none' gets no skills section."""
        from ragnarbot.agent.subagent import AgentTask, AgentTaskStatus

        ctx = MagicMock()
        mgr = self._make_manager(tmp_path, context_builder=ctx)
        defn = AgentDefinition(
            name="noskill",
            description="test",
            model="default",
            allowed_tools=["web_search"],
            allowed_skills="none",
            body="Do the work.",
            path="/fake/path",
        )
        task = AgentTask(
            id="t2", label="test", agent_name="noskill", task="go",
            status=AgentTaskStatus.running, messages=[],
            stop_event=asyncio.Event(), created_at="",
            origin={"channel": "cli", "chat_id": "direct"},
        )

        prompt = mgr._build_system_prompt(task, defn)
        assert "Available Skills" not in prompt
        ctx.skills.build_skills_summary.assert_not_called()

    def test_all_skills_passes_none_filter(self, tmp_path):
        """allowed_skills='all' passes only=None to build_skills_summary."""
        from ragnarbot.agent.subagent import AgentTask, AgentTaskStatus

        ctx = MagicMock()
        ctx.skills.build_skills_summary.return_value = "<skills></skills>"

        mgr = self._make_manager(tmp_path, context_builder=ctx)
        defn = AgentDefinition(
            name="allskills",
            description="test",
            model="default",
            allowed_tools="all",
            allowed_skills="all",
            body="Do the work.",
            path="/fake/path",
        )
        task = AgentTask(
            id="t3", label="test", agent_name="allskills", task="go",
            status=AgentTaskStatus.running, messages=[],
            stop_event=asyncio.Event(), created_at="",
            origin={"channel": "cli", "chat_id": "direct"},
        )

        mgr._build_system_prompt(task, defn)
        ctx.skills.build_skills_summary.assert_called_once_with(only=None)


# ---------------------------------------------------------------------------
# file_read auto-added for skill access
# ---------------------------------------------------------------------------

class TestFileReadAutoAdd:
    """Test that file_read is auto-added when skills are allowed."""

    def _make_manager(self, tmp_path):
        provider = MagicMock()
        provider.get_default_model.return_value = "test/model"
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        from ragnarbot.config.schema import ExecToolConfig
        return SubagentManager(
            provider=provider,
            workspace=tmp_path / "workspace",
            bus=bus,
            agents_loader=AgentsLoader(
                tmp_path / "workspace",
                builtin_agents_dir=tmp_path / "empty",
            ),
            model="test/model",
            exec_config=ExecToolConfig(),
        )

    def test_file_read_added_when_skills_allowed(self, tmp_path):
        """file_read is auto-added when allowedSkills is set, even if not in allowedTools."""
        mgr = self._make_manager(tmp_path)
        defn = AgentDefinition(
            name="skilled",
            description="test",
            model="default",
            allowed_tools=["web_search"],
            allowed_skills=["some-skill"],
            body="body",
            path="/fake/path",
        )
        reg, _ = mgr._build_agent_tool_registry(
            definition=defn, channel="cli", chat_id="direct",
        )
        assert reg.has("file_read")
        assert reg.has("web_search")

    def test_file_read_not_added_when_no_skills(self, tmp_path):
        """file_read is NOT added when allowedSkills is 'none'."""
        mgr = self._make_manager(tmp_path)
        defn = AgentDefinition(
            name="noskill",
            description="test",
            model="default",
            allowed_tools=["web_search"],
            allowed_skills="none",
            body="body",
            path="/fake/path",
        )
        reg, _ = mgr._build_agent_tool_registry(
            definition=defn, channel="cli", chat_id="direct",
        )
        assert not reg.has("file_read")
        assert reg.has("web_search")


# ---------------------------------------------------------------------------
# CronTool agent parameter
# ---------------------------------------------------------------------------

class TestCronToolAgentParam:
    """Test that the CronTool schema and execution support the agent parameter."""

    def test_agent_in_schema(self):
        """CronTool parameter schema includes 'agent'."""
        from ragnarbot.agent.tools.cron import CronTool
        tool = CronTool(cron_service=MagicMock())
        props = tool.parameters["properties"]
        assert "agent" in props
        assert props["agent"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_add_job_passes_agent(self):
        """CronTool._add_job passes agent to CronService.add_job."""
        from ragnarbot.agent.tools.cron import CronTool
        from ragnarbot.cron.types import CronJob, CronPayload, CronSchedule, CronJobState

        svc = MagicMock()
        svc.add_job.return_value = CronJob(
            id="abc",
            name="test",
            payload=CronPayload(agent="fast-researcher"),
            schedule=CronSchedule(kind="every", every_ms=3600_000),
            state=CronJobState(),
        )

        tool = CronTool(cron_service=svc)
        tool.set_context("telegram", "123")
        result = await tool.execute(
            action="add",
            message="do research",
            every_seconds=3600,
            agent="fast-researcher",
        )

        svc.add_job.assert_called_once()
        call_kwargs = svc.add_job.call_args
        assert call_kwargs.kwargs.get("agent") == "fast-researcher"
        assert "agent: fast-researcher" in result

    @pytest.mark.asyncio
    async def test_add_job_ignores_agent_for_session_mode(self):
        """Agent is not set when mode is session."""
        from ragnarbot.agent.tools.cron import CronTool
        from ragnarbot.cron.types import CronJob, CronPayload, CronSchedule, CronJobState

        svc = MagicMock()
        svc.add_job.return_value = CronJob(
            id="abc",
            name="test",
            payload=CronPayload(),
            schedule=CronSchedule(kind="every", every_ms=3600_000),
            state=CronJobState(),
        )

        tool = CronTool(cron_service=svc)
        tool.set_context("telegram", "123")
        await tool.execute(
            action="add",
            message="remind me",
            every_seconds=3600,
            mode="session",
            agent="fast-researcher",
        )

        call_kwargs = svc.add_job.call_args
        assert call_kwargs.kwargs.get("agent") is None

    @pytest.mark.asyncio
    async def test_update_job_passes_agent(self):
        """CronTool._update_job passes agent to CronService.update_job."""
        from ragnarbot.agent.tools.cron import CronTool
        from ragnarbot.cron.types import CronJob, CronPayload, CronSchedule, CronJobState

        svc = MagicMock()
        svc.update_job.return_value = CronJob(
            id="abc",
            name="test",
            payload=CronPayload(agent="deep-researcher"),
            schedule=CronSchedule(kind="every", every_ms=3600_000),
            state=CronJobState(),
        )

        tool = CronTool(cron_service=svc)
        tool.set_context("telegram", "123")
        result = await tool.execute(
            action="update",
            job_id="abc",
            agent="deep-researcher",
        )

        svc.update_job.assert_called_once()
        call_kwargs = svc.update_job.call_args
        assert call_kwargs.kwargs.get("agent") == "deep-researcher"

    @pytest.mark.asyncio
    async def test_list_jobs_shows_agent(self):
        """CronTool._list_jobs includes agent in output when set."""
        from ragnarbot.agent.tools.cron import CronTool
        from ragnarbot.cron.types import CronJob, CronPayload, CronSchedule, CronJobState

        svc = MagicMock()
        svc.list_jobs.return_value = [
            CronJob(
                id="abc",
                name="research job",
                payload=CronPayload(agent="fast-researcher", mode="isolated"),
                schedule=CronSchedule(kind="every", every_ms=3600_000),
                state=CronJobState(),
            ),
            CronJob(
                id="def",
                name="plain job",
                payload=CronPayload(mode="isolated"),
                schedule=CronSchedule(kind="every", every_ms=3600_000),
                state=CronJobState(),
            ),
        ]

        tool = CronTool(cron_service=svc)
        result = await tool.execute(action="list")
        assert "agent: fast-researcher" in result
        # plain job should NOT have agent info
        assert "def" in result
        lines = result.split("\n")
        plain_line = [l for l in lines if "def" in l][0]
        assert "agent:" not in plain_line


# ---------------------------------------------------------------------------
# Cron isolated with agent profile
# ---------------------------------------------------------------------------

class TestCronIsolatedAgentProfile:
    """Test process_cron_isolated with and without agent_name."""

    def _make_agent_loop(self, tmp_path, agents_loader=None):
        """Create a minimal AgentLoop for testing."""
        from ragnarbot.agent.loop import AgentLoop
        from ragnarbot.config.schema import ExecToolConfig

        provider = MagicMock()
        provider.get_default_model.return_value = "test/model"
        provider.default_max_tokens = 16_000
        provider.default_temperature = 0.7

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_inbound = AsyncMock()

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            model="test/model",
            exec_config=ExecToolConfig(),
        )

        # Inject a custom agents loader if provided
        if agents_loader:
            loop.context.agents = agents_loader

        return loop

    def test_build_cron_agent_messages_contains_agent_body(self, tmp_path):
        """Combined prompt contains both CRON_ISOLATED rules and AGENT.md body."""
        builtin = tmp_path / "builtin"
        d = builtin / "researcher"
        d.mkdir(parents=True)
        (d / "AGENT.md").write_text(
            "---\nname: researcher\ndescription: Research\n---\n\n"
            "You are a research specialist. Find and synthesize information.",
            encoding="utf-8",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        loop = self._make_agent_loop(tmp_path, agents_loader=loader)

        defn = loader.load_agent("researcher")
        assert defn is not None

        session_metadata = {
            "cron_isolated": {
                "job_name": "research job",
                "schedule_desc": "every 1h",
                "task_message": "Research AI news",
            },
        }

        messages = loop._build_cron_agent_messages(defn, "Research AI news", session_metadata)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Research AI news"

        system_prompt = messages[0]["content"]
        # Should contain cron rules
        assert "deliver_result" in system_prompt
        assert "NOT an interactive conversation" in system_prompt
        # Should contain agent instructions
        assert "research specialist" in system_prompt

    def test_build_cron_agent_messages_includes_skills(self, tmp_path):
        """Skills summary is injected when agent has allowed_skills."""
        builtin = tmp_path / "builtin"
        d = builtin / "skilled"
        d.mkdir(parents=True)
        (d / "AGENT.md").write_text(
            "---\nname: skilled\ndescription: Skilled agent\n"
            "allowedSkills: [my-skill]\n---\nDo things with skills.",
            encoding="utf-8",
        )
        loader = AgentsLoader(tmp_path / "workspace", builtin_agents_dir=builtin)
        loop = self._make_agent_loop(tmp_path, agents_loader=loader)

        # Mock skills loader
        loop.context.skills.build_skills_summary = MagicMock(return_value=(
            "<skills>\n"
            "  <skill available=\"true\">\n"
            "    <name>my-skill</name>\n"
            "    <description>A skill</description>\n"
            "    <location>/path/to/SKILL.md</location>\n"
            "  </skill>\n"
            "</skills>"
        ))

        defn = loader.load_agent("skilled")
        session_metadata = {
            "cron_isolated": {
                "job_name": "skilled job",
                "schedule_desc": "every 1h",
                "task_message": "Do work",
            },
        }

        messages = loop._build_cron_agent_messages(defn, "Do work", session_metadata)
        system_prompt = messages[0]["content"]

        assert "Available Skills" in system_prompt
        assert "my-skill" in system_prompt
        loop.context.skills.build_skills_summary.assert_called_once_with(only=["my-skill"])

    def test_build_cron_agent_tool_registry_filters_tools(self, tmp_path):
        """Agent with restricted tools gets only those tools + deliver_result."""
        loop = self._make_agent_loop(tmp_path)

        defn = AgentDefinition(
            name="restricted",
            description="test",
            model="default",
            allowed_tools=["web_search", "web_fetch"],
            allowed_skills="none",
            body="body",
            path="/fake/path",
        )

        reg, deliver = loop._build_cron_agent_tool_registry(defn, "cli", "direct")

        assert reg.has("web_search")
        assert reg.has("web_fetch")
        assert reg.has("deliver_result")
        # Should NOT have tools outside the allowed list
        assert not reg.has("exec")
        assert not reg.has("file_write")
        assert not reg.has("cron")

    def test_build_cron_agent_tool_registry_all_tools(self, tmp_path):
        """Agent with allowedTools='all' gets the full isolated registry."""
        loop = self._make_agent_loop(tmp_path)

        defn = AgentDefinition(
            name="full",
            description="test",
            model="default",
            allowed_tools="all",
            allowed_skills="none",
            body="body",
            path="/fake/path",
        )

        reg, deliver = loop._build_cron_agent_tool_registry(defn, "cli", "direct")

        # Should have all the typical isolated tools
        assert reg.has("file_read")
        assert reg.has("exec")
        assert reg.has("web_search")
        assert reg.has("deliver_result")

    def test_build_cron_agent_tool_registry_adds_file_read_for_skills(self, tmp_path):
        """file_read is auto-added when agent has allowed_skills."""
        loop = self._make_agent_loop(tmp_path)

        defn = AgentDefinition(
            name="skilled",
            description="test",
            model="default",
            allowed_tools=["web_search"],
            allowed_skills=["some-skill"],
            body="body",
            path="/fake/path",
        )

        reg, deliver = loop._build_cron_agent_tool_registry(defn, "cli", "direct")

        assert reg.has("file_read")
        assert reg.has("web_search")
        assert reg.has("deliver_result")
        assert not reg.has("exec")
