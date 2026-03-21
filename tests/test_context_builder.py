"""Tests for ContextBuilder built-in file loading and prompt assembly."""

from datetime import datetime, timedelta

import pytest

from ragnarbot.agent.context import BUILTIN_DIR, ContextBuilder
from ragnarbot.agent.memory import MemoryStore
from ragnarbot.agent.prompt_overlays import (
    OPENAI_STYLE_ADDENDUM,
    get_model_behavior_addendum,
    is_openai_family_model,
)


@pytest.fixture(autouse=True)
def default_profile(monkeypatch):
    monkeypatch.setenv("RAGNARBOT_PROFILE", "default")


class TestBuiltinFilesExist:
    """Verify built-in markdown files are present in the package."""

    @pytest.mark.parametrize("filename", ContextBuilder.BUILTIN_FILES)
    def test_builtin_file_exists(self, filename):
        assert (BUILTIN_DIR / filename).exists()

    def test_telegram_builtin_exists(self):
        assert (BUILTIN_DIR / "TELEGRAM.md").exists()


class TestLoadBuiltinFiles:
    def _make_builder(self, tmp_path):
        return ContextBuilder(tmp_path / "workspace")

    def test_loads_all_builtin_files(self, tmp_path):
        cb = self._make_builder(tmp_path)
        result = cb._load_builtin_files()
        assert "# Soul" in result
        assert "# Operations Manual" in result
        assert "# Built-in Tools" in result

    def test_placeholders_replaced(self, tmp_path):
        cb = self._make_builder(tmp_path)
        result = cb._load_builtin_files()
        workspace_path = str(cb.workspace.expanduser().resolve())
        assert workspace_path in result
        assert "{workspace_path}" not in result
        assert "{timezone}" not in result
        assert "{data_root}" not in result

    def test_escaped_braces_preserved(self, tmp_path):
        cb = self._make_builder(tmp_path)
        result = cb._load_builtin_files()
        # {skill-name} should be literal after escaping
        assert "{skill-name}" in result


class TestPromptOverlays:
    def test_identifies_openai_family_models(self):
        assert is_openai_family_model("openai/gpt-5.4") is True
        assert is_openai_family_model("gpt-5.4") is True
        assert is_openai_family_model("openrouter/openai/gpt-5.4") is True
        assert is_openai_family_model("anthropic/claude-sonnet-4-5") is False

    def test_returns_addendum_only_for_openai_family_models(self):
        assert get_model_behavior_addendum("openai/gpt-5.4") == OPENAI_STYLE_ADDENDUM
        assert get_model_behavior_addendum("anthropic/claude-sonnet-4-5") == ""


class TestLoadBuiltinTelegram:
    def _make_builder(self, tmp_path):
        return ContextBuilder(tmp_path / "workspace")

    def test_telegram_placeholders(self, tmp_path):
        cb = self._make_builder(tmp_path)
        user_data = {
            "first_name": "John",
            "last_name": "Doe",
            "username": "johndoe",
            "user_id": "123456",
        }
        result = cb._load_builtin_telegram(user_data)
        assert "John Doe" in result
        assert "@johndoe" in result
        assert "123456" in result

    def test_telegram_missing_fields(self, tmp_path):
        cb = self._make_builder(tmp_path)
        user_data = {}
        result = cb._load_builtin_telegram(user_data)
        assert "Unknown" in result
        assert "N/A" in result


class TestIsolatedBuiltins:
    def test_cron_isolated_includes_workspace_path(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")

        result = cb._load_builtin_cron_isolated({
            "job_name": "job",
            "schedule_desc": "every 1h",
            "task_message": "Do work",
        })

        assert f"**Workspace:** {cb.workspace.expanduser().resolve()}" in result
        assert "absolute path under the workspace above" in result

    def test_heartbeat_isolated_includes_workspace_path(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")

        result = cb._load_builtin_heartbeat_isolated({
            "tasks_summary": "Task A",
        })

        assert f"**Workspace:** {cb.workspace.expanduser().resolve()}" in result
        assert "If you mention a file, include its absolute path" in result


class TestBootstrapFiles:
    def test_user_files_have_path_header(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        result = cb._load_bootstrap_files()
        workspace_path = str(cb.workspace.expanduser().resolve())
        assert f"## IDENTITY.md\n> Path: {workspace_path}/IDENTITY.md" in result

    def test_user_files_no_agents_or_soul(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        result = cb._load_bootstrap_files()
        assert "## AGENTS.md" not in result
        assert "## SOUL.md" not in result


class TestBuildSystemPrompt:
    def test_assembly_order(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        prompt = cb.build_system_prompt()
        # Identity header comes first
        ragnarbot_pos = prompt.index("# ragnarbot")
        # Soul comes after identity
        soul_pos = prompt.index("# Soul")
        # Operations Manual comes after Soul
        ops_pos = prompt.index("# Operations Manual")
        # Built-in Tools comes after Operations
        tools_pos = prompt.index("# Built-in Tools")
        # User files come after built-in
        identity_pos = prompt.index("## IDENTITY.md")
        assert ragnarbot_pos < soul_pos < ops_pos < tools_pos < identity_pos

    def test_no_old_identity_content(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        prompt = cb.build_system_prompt()
        # Old verbose identity content should not be present
        assert "You have access to tools that allow you to:" not in prompt
        assert "IMPORTANT: When responding to direct questions" not in prompt

    def test_telegram_included_when_channel_matches(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        prompt = cb.build_system_prompt(
            channel="telegram",
            session_metadata={
                "user_data": {
                    "first_name": "Test",
                    "username": "testuser",
                    "user_id": "999",
                }
            },
        )
        assert "# Telegram Context" in prompt
        assert "Test" in prompt

    def test_telegram_excluded_for_other_channels(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        prompt = cb.build_system_prompt(channel="cli")
        assert "# Telegram Context" not in prompt

    def test_includes_today_and_yesterday_memory(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        memory = MemoryStore(cb.workspace)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        memory.write_long_term("# Long-term Memory\n\n- durable fact")
        memory.write_day(today, f"# {today}\n\n- today item")
        memory.write_day(yesterday, f"# {yesterday}\n\n- yesterday item")

        prompt = cb.build_system_prompt()
        assert "## Long-term Memory" in prompt
        assert "## Today's Notes" in prompt
        assert "## Yesterday's Notes" in prompt
        assert "today item" in prompt
        assert "yesterday item" in prompt

    def test_custom_profile_uses_runtime_name_and_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAGNARBOT_PROFILE", "vodichezka")
        cb = ContextBuilder(tmp_path / "workspace")
        prompt = cb.build_system_prompt()
        assert "# ragnarbot-vodichezka" in prompt
        assert "~/.ragnarbot-vodichezka/browser-profile/" in prompt
        assert "~/.ragnarbot/browser-profile/" not in prompt
        assert "~/.ragnarbot/workspace" not in prompt

    def test_includes_openai_behavior_addendum_for_openai_models(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        cb.model = "openai/gpt-5.4"

        prompt = cb.build_system_prompt()

        assert OPENAI_STYLE_ADDENDUM in prompt

    def test_omits_openai_behavior_addendum_for_non_openai_models(self, tmp_path):
        cb = ContextBuilder(tmp_path / "workspace")
        cb.model = "anthropic/claude-sonnet-4-5"

        prompt = cb.build_system_prompt()

        assert OPENAI_STYLE_ADDENDUM not in prompt
