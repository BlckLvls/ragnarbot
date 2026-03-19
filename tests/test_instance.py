"""Tests for profile-aware instance resolution."""

import importlib.util
import json
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from ragnarbot.instance import (
    GatewayClaimError,
    acquire_gateway_claim,
    data_root_for_profile,
    ensure_instance_root,
    get_instance,
    get_live_gateway_pid,
    instance_profiles_on_disk,
    load_gateway_claim,
    normalize_profile_name,
    release_gateway_claim,
    resolve_active_profile,
    signal_live_gateway,
    workspace_config_value,
)


@pytest.fixture(autouse=True)
def reset_profile(monkeypatch):
    monkeypatch.delenv("RAGNARBOT_PROFILE", raising=False)


def test_default_profile_root(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        assert data_root_for_profile() == tmp_path / ".ragnarbot"
        assert workspace_config_value() == "~/.ragnarbot/workspace"


def test_custom_profile_root(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        info = get_instance("vodichezka")
        assert info.profile == "vodichezka"
        assert info.runtime_name == "ragnarbot-vodichezka"
        assert info.data_root == tmp_path / ".ragnarbot-vodichezka"
        assert workspace_config_value("vodichezka") == "~/.ragnarbot-vodichezka/workspace"


def test_env_profile_used_when_explicit_missing(monkeypatch):
    monkeypatch.setenv("RAGNARBOT_PROFILE", "vodichezka")
    assert resolve_active_profile() == "vodichezka"


def test_explicit_profile_overrides_env(monkeypatch):
    monkeypatch.setenv("RAGNARBOT_PROFILE", "vodichezka")
    assert resolve_active_profile("default") == "default"


@pytest.mark.parametrize("value", ["", "bad/name", "bad.name", "..", "UPPER.CASE"])
def test_invalid_profile_names_rejected(value):
    with pytest.raises(ValueError):
        normalize_profile_name(value)


def test_profiles_on_disk(tmp_path):
    (tmp_path / ".ragnarbot").mkdir()
    (tmp_path / ".ragnarbot-vodichezka").mkdir()
    (tmp_path / "other").mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        assert instance_profiles_on_disk() == ["default", "vodichezka"]


def test_acquire_gateway_claim_writes_claim_and_pid(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        claim = acquire_gateway_claim(pid=1234)
        info = get_instance()
        assert claim["pid"] == 1234
        assert load_gateway_claim()["pid"] == 1234
        assert info.pid_path.read_text() == "1234"
        release_gateway_claim(pid=1234)


def test_valid_gateway_claim_blocks_duplicate_start(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        acquire_gateway_claim(pid=1234)
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr("ragnarbot.instance.gateway_process_matches", lambda *args, **kwargs: True)
            with pytest.raises(GatewayClaimError) as exc:
                acquire_gateway_claim(pid=5678)
        assert exc.value.pid == 1234
        release_gateway_claim(pid=1234)


def test_stale_gateway_claim_is_reclaimed(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        info = ensure_instance_root()
        info.gateway_claim_path.write_text(json.dumps({
            "pid": 1234,
            "profile": "default",
            "runtime_role": "gateway",
        }))
        info.pid_path.write_text("1234")
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr("ragnarbot.instance.gateway_process_matches", lambda *args, **kwargs: False)
            claim = acquire_gateway_claim(pid=5678)
        assert claim["pid"] == 5678
        assert load_gateway_claim()["pid"] == 5678
        release_gateway_claim(pid=5678)


def test_self_restart_accepts_claim_owned_by_current_pid(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        acquire_gateway_claim(pid=1234)
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr("ragnarbot.instance.gateway_process_matches", lambda *args, **kwargs: True)
            claim = acquire_gateway_claim(pid=1234)
        assert claim["pid"] == 1234
        release_gateway_claim(pid=1234)


def test_get_live_gateway_pid_cleans_stale_claim(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        info = ensure_instance_root()
        info.gateway_claim_path.write_text(json.dumps({
            "pid": 1234,
            "profile": "default",
            "runtime_role": "gateway",
        }))
        info.pid_path.write_text("1234")
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr("ragnarbot.instance.gateway_process_matches", lambda *args, **kwargs: False)
            assert get_live_gateway_pid() is None
        assert not info.gateway_claim_path.exists()
        assert not info.pid_path.exists()


def test_signal_live_gateway_only_targets_valid_claim(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
        acquire_gateway_claim(pid=2222)
        with (
            patch("ragnarbot.instance.gateway_process_matches", return_value=True),
            patch("ragnarbot.instance.os.kill") as mock_kill,
        ):
            assert signal_live_gateway(signal.SIGUSR1) == 2222
        mock_kill.assert_called_once_with(2222, signal.SIGUSR1)
        release_gateway_claim(pid=2222)


def _load_script_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_creator_default_path_is_lazy(tmp_path, monkeypatch):
    module = _load_script_module(
        Path("/Users/lvls/ragnarbot/ragnarbot/skills/skill-creator/scripts/init_skill.py"),
        "test_init_skill",
    )
    monkeypatch.setenv("RAGNARBOT_PROFILE", "vodichezka")
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
    assert module.default_path() == tmp_path / ".ragnarbot-vodichezka" / "workspace" / "skills"


def test_agent_creator_default_path_is_lazy(tmp_path, monkeypatch):
    module = _load_script_module(
        Path("/Users/lvls/ragnarbot/ragnarbot/skills/agent-creator/scripts/init_agent.py"),
        "test_init_agent",
    )
    monkeypatch.setenv("RAGNARBOT_PROFILE", "vodichezka")
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: tmp_path)
    assert module.default_path() == tmp_path / ".ragnarbot-vodichezka" / "workspace" / "agents"
