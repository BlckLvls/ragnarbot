"""Tests for ContextBuilder built-in file loading and prompt assembly."""

from pathlib import Path

import pytest

from ragnarbot.agent.context import BUILTIN_DIR, ContextBuilder


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

    def test_escaped_braces_preserved(self, tmp_path):
        cb = self._make_builder(tmp_path)
        result = cb._load_builtin_files()
        # {skill-name} should be literal after escaping
        assert "{skill-name}" in result


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
