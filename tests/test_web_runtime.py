"""Tests for Web UI runtime discovery and profile-aware ports."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer

from ragnarbot.cli import commands
from ragnarbot.config.loader import convert_to_camel, load_config
from ragnarbot.config.ports import assign_profile_ports
from ragnarbot.config.schema import Config
from ragnarbot.instance import get_instance
from ragnarbot.web.runtime import WebProbe, lan_web_url, port_is_available, probe_web, web_url


def _web_config(host: str = "0.0.0.0", port: int = 18792):
    return SimpleNamespace(host=host, port=port, enabled=True)


def test_web_url_turns_wildcard_bind_into_local_browser_url():
    assert web_url(_web_config()) == "http://127.0.0.1:18792"


def test_web_url_formats_ipv6_hosts():
    assert web_url("2001:db8::1", 18792) == "http://[2001:db8::1]:18792"


def test_lan_url_is_only_available_for_wildcard_binds():
    with patch("ragnarbot.web.runtime.lan_ip", return_value="192.168.1.50"):
        assert lan_web_url(_web_config()) == "http://192.168.1.50:18792"
        assert lan_web_url(_web_config(host="127.0.0.1")) is None


def test_probe_web_rejects_a_different_profile():
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({"profile": "work"}).encode()
    response.__enter__.return_value = response

    with patch("ragnarbot.web.runtime.urlopen", return_value=response):
        probe = probe_web(_web_config(), expected_profile="default")

    assert probe == WebProbe(
        reachable=False,
        profile="work",
        error="port is serving profile 'work'",
    )


def test_port_availability_detects_a_live_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert port_is_available("127.0.0.1", port) is False
    assert port_is_available("127.0.0.1", port) is True


def _write_profile_config(profile: str, config: Config) -> None:
    path = get_instance(profile).config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(convert_to_camel(config.model_dump())), encoding="utf-8")


def test_new_profiles_receive_stable_non_overlapping_port_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
    _write_profile_config("default", Config())

    with patch("ragnarbot.config.ports.port_is_available", return_value=True):
        work = assign_profile_ports(Config(), profile="work")

    assert (work.gateway.port, work.hooks.port, work.web.port) == (18800, 18801, 18802)
    _write_profile_config("work", work)

    with patch("ragnarbot.config.ports.port_is_available", return_value=True):
        personal = assign_profile_ports(Config(), profile="personal")

    assert (personal.gateway.port, personal.hooks.port, personal.web.port) == (
        18810,
        18811,
        18812,
    )


def test_profile_allocation_skips_an_occupied_port_block(tmp_path, monkeypatch):
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
    _write_profile_config("default", Config())

    with patch(
        "ragnarbot.config.ports.port_is_available",
        side_effect=[False, True, True],
    ):
        config = assign_profile_ports(Config(), profile="work")

    assert (config.gateway.port, config.hooks.port, config.web.port) == (18810, 18811, 18812)


def test_missing_custom_profile_config_uses_allocated_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGNARBOT_PROFILE", "work")
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
    _write_profile_config("default", Config())

    with patch("ragnarbot.config.ports.port_is_available", return_value=True):
        config = load_config()

    assert config.web.host == "0.0.0.0"
    assert (config.gateway.port, config.hooks.port, config.web.port) == (18800, 18801, 18802)
    assert get_instance("work").config_path.exists()

    with patch("ragnarbot.config.ports.port_is_available", return_value=False):
        reloaded = load_config()

    assert (reloaded.gateway.port, reloaded.hooks.port, reloaded.web.port) == (
        18800,
        18801,
        18802,
    )


def test_runtime_port_conflict_reports_owning_profile():
    config = Config()
    console = MagicMock()
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "port_is_available", side_effect=[True, False]),
        patch.object(commands, "probe_web", return_value=WebProbe(False, profile="work")),
        pytest.raises(typer.Exit),
    ):
        commands._ensure_runtime_ports_available(config)

    output = "\n".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "web 0.0.0.0:18792 (profile 'work')" in output


def test_print_web_runtime_shows_browser_url_and_bind_address():
    config = Config()
    console = MagicMock()
    instance = SimpleNamespace(profile="default")
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "get_instance", return_value=instance),
        patch.object(commands, "probe_web", return_value=WebProbe(True, profile="default")),
        patch.object(commands, "lan_web_url", return_value="http://192.168.1.50:18792"),
    ):
        commands._print_web_runtime(config)

    output = "\n".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "http://127.0.0.1:18792" in output
    assert "http://192.168.1.50:18792" in output
    assert "0.0.0.0:18792" in output
    assert "reachable" in output


def test_web_url_command_prints_only_the_browser_url():
    config = Config()
    console = MagicMock()
    with (
        patch.object(commands, "console", console),
        patch("ragnarbot.config.loader.load_config", return_value=config),
    ):
        commands.web_url_command(lan=False)

    console.print.assert_called_once_with("http://127.0.0.1:18792", markup=False)


def test_web_url_command_can_print_lan_address():
    config = Config()
    console = MagicMock()
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "lan_web_url", return_value="http://192.168.1.50:18792"),
        patch("ragnarbot.config.loader.load_config", return_value=config),
    ):
        commands.web_url_command(lan=True)

    console.print.assert_called_once_with("http://192.168.1.50:18792", markup=False)


def test_web_open_checks_profile_and_opens_browser():
    config = Config()
    console = MagicMock()
    instance = SimpleNamespace(profile="default")
    with (
        patch.object(commands, "console", console),
        patch.object(commands, "get_instance", return_value=instance),
        patch.object(commands, "probe_web", return_value=WebProbe(True, profile="default")),
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("webbrowser.open", return_value=True) as mock_open,
    ):
        commands.web_open()

    mock_open.assert_called_once_with("http://127.0.0.1:18792")
