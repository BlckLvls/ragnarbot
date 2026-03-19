"""Tests for CLI helpers around gateway ownership and pending updates."""

from unittest.mock import MagicMock, patch

from ragnarbot.cli import commands


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
