"""Tests for CLI helpers around gateway ownership and pending updates."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from ragnarbot.auth.credentials import Credentials
from ragnarbot.cli import commands
from ragnarbot.config.schema import Config


def test_reconcile_pending_update_clears_restart_required_payload(monkeypatch):
    monkeypatch.setattr(commands, "__version__", "0.4.0")
    payload = {
        "requires_restart": True,
        "new_version": "0.4.0",
    }

    with (
        patch.object(commands, "load_pending_update", return_value=payload),
        patch.object(commands, "clear_pending_update") as mock_clear,
    ):
        assert commands._reconcile_pending_update_after_startup() is None

    mock_clear.assert_called_once()


def test_reconcile_pending_update_keeps_info_only_payload(monkeypatch):
    monkeypatch.setattr(commands, "__version__", "0.4.0")
    payload = {
        "requires_restart": False,
        "new_version": "0.4.0",
    }

    with (
        patch.object(commands, "load_pending_update", return_value=payload),
        patch.object(commands, "clear_pending_update") as mock_clear,
    ):
        assert commands._reconcile_pending_update_after_startup() == payload

    mock_clear.assert_not_called()


def test_print_pending_update_shows_pending_notice():
    console = MagicMock()
    payload = {
        "requires_restart": False,
        "old_version": "0.3.0",
        "new_version": "0.4.0",
    }

    with (
        patch.object(commands, "console", console),
        patch.object(commands, "load_pending_update", return_value=payload),
    ):
        commands._print_pending_update()

    rendered = console.print.call_args.args[0]
    assert "pending notice" in rendered


def test_signal_gateway_reload_signals_valid_claim():
    console = MagicMock()
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "signal_live_gateway", return_value=4242) as mock_signal,
    ):
        commands._signal_gateway_reload()

    mock_signal.assert_called_once()
    assert "Signaled gateway" in console.print.call_args.args[0]


def test_signal_gateway_reload_handles_stale_claim():
    console = MagicMock()
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "signal_live_gateway", return_value=None),
    ):
        commands._signal_gateway_reload()

    assert "claim is stale" in console.print.call_args.args[0]


def test_status_shows_lightning_no_effect_note(tmp_path):
    console = MagicMock()
    config = Config()
    config.agents.defaults.model = "anthropic/claude-opus-4-6"
    config.agents.defaults.auth_method = "api_key"
    config.agents.defaults.lightning_mode = True

    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "creds.json"
    workspace = tmp_path / "workspace"
    config_path.write_text("{}", encoding="utf-8")
    creds_path.write_text("{}", encoding="utf-8")
    workspace.mkdir()

    instance = MagicMock()
    instance.runtime_name = "ragnarbot"
    instance.profile = "default"
    instance.data_root = Path(tmp_path)

    with (
        patch.object(commands, "console", console),
        patch.object(commands, "get_instance", return_value=instance),
        patch("ragnarbot.config.loader.get_config_path", return_value=config_path),
        patch("ragnarbot.auth.credentials.get_credentials_path", return_value=creds_path),
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("ragnarbot.auth.credentials.load_credentials", return_value=Credentials()),
    ):
        commands.status()

    rendered = "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
    assert "Lightning: Enabled" in rendered
    assert "no effect for current model/auth" in rendered


def test_status_omits_lightning_no_effect_note_for_openai_oauth(tmp_path):
    console = MagicMock()
    config = Config()
    config.agents.defaults.model = "openai/gpt-5.4"
    config.agents.defaults.auth_method = "oauth"
    config.agents.defaults.lightning_mode = True

    config_path = tmp_path / "config.json"
    creds_path = tmp_path / "creds.json"
    workspace = tmp_path / "workspace"
    config_path.write_text("{}", encoding="utf-8")
    creds_path.write_text("{}", encoding="utf-8")
    workspace.mkdir()

    instance = MagicMock()
    instance.runtime_name = "ragnarbot"
    instance.profile = "default"
    instance.data_root = Path(tmp_path)

    with (
        patch.object(commands, "console", console),
        patch.object(commands, "get_instance", return_value=instance),
        patch("ragnarbot.config.loader.get_config_path", return_value=config_path),
        patch("ragnarbot.auth.credentials.get_credentials_path", return_value=creds_path),
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("ragnarbot.auth.credentials.load_credentials", return_value=Credentials()),
        patch("ragnarbot.auth.openai_oauth.is_authenticated", return_value=True),
    ):
        commands.status()

    rendered = "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
    assert "Lightning: Enabled" in rendered
    assert "no effect for current model/auth" not in rendered
