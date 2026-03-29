"""Tests for the hooks subsystem."""

import json

import pytest

from ragnarbot.hooks.service import HookService, _generate_hook_id
from ragnarbot.hooks.types import HookDefinition, HookStore

# ========== Types ==========


def test_hook_definition_defaults():
    hook = HookDefinition(id="hk_test", name="test", instructions="do stuff")
    assert hook.mode == "alert"
    assert hook.enabled is True
    assert hook.trigger_count == 0
    assert hook.channel is None
    assert hook.to is None


def test_hook_store_defaults():
    store = HookStore()
    assert store.version == 1
    assert store.hooks == []


# ========== ID Generation ==========


def test_hook_id_format():
    hid = _generate_hook_id()
    assert hid.startswith("hk_")
    assert len(hid) > 20  # cryptographic token should be long


def test_hook_id_uniqueness():
    ids = {_generate_hook_id() for _ in range(100)}
    assert len(ids) == 100


# ========== Service CRUD ==========


@pytest.fixture
def hook_service(tmp_path):
    store_path = tmp_path / "hooks.json"
    logs_dir = tmp_path / "logs"
    return HookService(store_path, logs_dir)


def test_add_hook(hook_service):
    hook = hook_service.add_hook(
        name="CI alerts",
        instructions="Summarize the CI payload.",
        mode="alert",
        channel="telegram",
        to="12345",
    )
    assert hook.name == "CI alerts"
    assert hook.instructions == "Summarize the CI payload."
    assert hook.mode == "alert"
    assert hook.id.startswith("hk_")
    assert hook.channel == "telegram"
    assert hook.to == "12345"
    assert hook.created_at_ms > 0


def test_get_hook(hook_service):
    hook = hook_service.add_hook("test", "instructions")
    found = hook_service.get_hook(hook.id)
    assert found is not None
    assert found.name == "test"


def test_get_hook_not_found(hook_service):
    assert hook_service.get_hook("nonexistent") is None


def test_list_hooks(hook_service):
    hook_service.add_hook("a", "inst a")
    hook_service.add_hook("b", "inst b")
    hooks = hook_service.list_hooks()
    assert len(hooks) == 2


def test_list_hooks_excludes_disabled(hook_service):
    h = hook_service.add_hook("a", "inst")
    hook_service.update_hook(h.id, enabled=False)
    assert len(hook_service.list_hooks(include_disabled=False)) == 0
    assert len(hook_service.list_hooks(include_disabled=True)) == 1


def test_update_hook(hook_service):
    hook = hook_service.add_hook("old", "old inst")
    updated = hook_service.update_hook(
        hook.id, name="new", instructions="new inst", mode="silent",
    )
    assert updated is not None
    assert updated.name == "new"
    assert updated.instructions == "new inst"
    assert updated.mode == "silent"


def test_update_hook_not_found(hook_service):
    assert hook_service.update_hook("nonexistent", name="x") is None


def test_delete_hook(hook_service):
    hook = hook_service.add_hook("to delete", "inst")
    assert hook_service.delete_hook(hook.id) is True
    assert hook_service.get_hook(hook.id) is None


def test_delete_hook_not_found(hook_service):
    assert hook_service.delete_hook("nonexistent") is False


def test_increment_trigger_count(hook_service):
    hook = hook_service.add_hook("test", "inst")
    assert hook.trigger_count == 0
    hook_service.increment_trigger_count(hook.id)
    updated = hook_service.get_hook(hook.id)
    assert updated.trigger_count == 1


# ========== Persistence ==========


def test_store_roundtrip(tmp_path):
    store_path = tmp_path / "hooks.json"
    logs_dir = tmp_path / "logs"

    svc1 = HookService(store_path, logs_dir)
    svc1.add_hook("hook1", "instructions1", mode="alert", channel="tg", to="123")
    svc1.add_hook("hook2", "instructions2", mode="silent")

    # Force reload from disk
    svc2 = HookService(store_path, logs_dir)
    hooks = svc2.list_hooks(include_disabled=True)
    assert len(hooks) == 2
    assert hooks[0].name == "hook1"
    assert hooks[0].mode == "alert"
    assert hooks[0].channel == "tg"
    assert hooks[1].name == "hook2"
    assert hooks[1].mode == "silent"


def test_store_camel_case_keys(tmp_path):
    store_path = tmp_path / "hooks.json"
    logs_dir = tmp_path / "logs"

    svc = HookService(store_path, logs_dir)
    svc.add_hook("test", "inst")

    data = json.loads(store_path.read_text())
    hook_data = data["hooks"][0]
    assert "createdAtMs" in hook_data
    assert "updatedAtMs" in hook_data
    assert "triggerCount" in hook_data


# ========== Logging ==========


def test_log_trigger(hook_service):
    hook = hook_service.add_hook("test", "inst")
    hook_service.log_trigger(
        hook, '{"event": "test"}', "ok", 1.5, "result text",
    )

    log_file = hook_service.logs_dir / f"{hook.id}.jsonl"
    assert log_file.exists()

    entries = hook_service.get_history(hook.id)
    assert len(entries) == 1
    assert entries[0]["status"] == "ok"
    assert entries[0]["duration_s"] == 1.5


def test_get_history_limit(hook_service):
    hook = hook_service.add_hook("test", "inst")
    for i in range(20):
        hook_service.log_trigger(hook, f"payload {i}", "ok", 0.1)

    entries = hook_service.get_history(hook.id, limit=5)
    assert len(entries) == 5


def test_get_history_empty(hook_service):
    assert hook_service.get_history("nonexistent") == []


# ========== HookTool ==========


@pytest.mark.asyncio
async def test_hook_tool_create(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    tool.set_context("telegram", "12345")

    result = await tool.execute(
        action="create",
        name="CI Monitor",
        instructions="Summarize CI failures.",
    )
    assert "Created hook 'CI Monitor'" in result
    assert "/hooks/hk_" in result
    assert "curl" in result


@pytest.mark.asyncio
async def test_hook_tool_create_missing_name(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    tool.set_context("telegram", "12345")

    result = await tool.execute(action="create", instructions="test")
    assert "Error" in result


@pytest.mark.asyncio
async def test_hook_tool_create_no_context(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)

    result = await tool.execute(
        action="create", name="test", instructions="test",
    )
    assert "Error" in result


@pytest.mark.asyncio
async def test_hook_tool_list(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    tool.set_context("telegram", "12345")

    await tool.execute(action="create", name="Hook A", instructions="inst a")
    await tool.execute(action="create", name="Hook B", instructions="inst b")

    result = await tool.execute(action="list")
    assert "Hook A" in result
    assert "Hook B" in result


@pytest.mark.asyncio
async def test_hook_tool_list_empty(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    result = await tool.execute(action="list")
    assert "No registered hooks" in result


@pytest.mark.asyncio
async def test_hook_tool_delete(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    tool.set_context("telegram", "12345")

    await tool.execute(action="create", name="To Delete", instructions="inst")
    hooks = hook_service.list_hooks()
    hook_id = hooks[0].id

    result = await tool.execute(action="delete", id=hook_id)
    assert "Deleted hook" in result


@pytest.mark.asyncio
async def test_hook_tool_history(hook_service):
    from ragnarbot.agent.tools.hook import HookTool
    tool = HookTool(hook_service)
    tool.set_context("telegram", "12345")

    await tool.execute(action="create", name="Hist Hook", instructions="inst")
    hooks = hook_service.list_hooks()
    hook = hooks[0]

    hook_service.log_trigger(hook, "payload", "ok", 0.5)

    result = await tool.execute(action="history", id=hook.id)
    assert "triggers" in result.lower()
    assert "ok" in result
