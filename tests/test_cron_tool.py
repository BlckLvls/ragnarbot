"""Tests for CronTool — after param, past-time rejection, schema."""

import datetime
from datetime import timedelta

import pytest

from ragnarbot.agent.tools.cron import CronTool
from ragnarbot.cron.service import CronService


@pytest.fixture
def cron_tool(tmp_path):
    """Create a CronTool backed by a real CronService in a temp directory."""
    store_path = tmp_path / "cron.json"
    service = CronService(store_path=store_path)
    tool = CronTool(service)
    tool.set_context(channel="test", chat_id="123")
    return tool


@pytest.mark.asyncio
async def test_after_creates_one_shot_job(cron_tool):
    """after=300 should create a one-shot job ~300s in the future."""
    before = datetime.datetime.now()
    result = await cron_tool.execute(action="add", message="retry task", after=300)
    after = datetime.datetime.now()

    assert "Created job" in result

    # Verify the job was actually created with correct timing
    jobs = cron_tool._cron.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.schedule.kind == "at"

    expected_min = int((before + timedelta(seconds=300)).timestamp() * 1000)
    expected_max = int((after + timedelta(seconds=300)).timestamp() * 1000)
    assert expected_min <= job.schedule.at_ms <= expected_max


@pytest.mark.asyncio
async def test_after_below_minimum_returns_error(cron_tool):
    """after=5 should be rejected (minimum is 10)."""
    result = await cron_tool.execute(action="add", message="too soon", after=5)
    assert "Error" in result
    assert "at least 10" in result

    # No job should be created
    assert len(cron_tool._cron.list_jobs()) == 0


@pytest.mark.asyncio
async def test_at_past_datetime_returns_error(cron_tool):
    """Scheduling with 'at' in the past should return an error."""
    past = (datetime.datetime.now() - timedelta(hours=1)).isoformat()
    result = await cron_tool.execute(action="add", message="too late", at=past)
    assert "Error" in result
    assert "past" in result

    assert len(cron_tool._cron.list_jobs()) == 0


@pytest.mark.asyncio
async def test_at_future_datetime_succeeds(cron_tool):
    """Scheduling with 'at' in the future should work."""
    future = (datetime.datetime.now() + timedelta(hours=1)).isoformat()
    result = await cron_tool.execute(action="add", message="future task", at=future)
    assert "Created job" in result
    assert len(cron_tool._cron.list_jobs()) == 1


@pytest.mark.asyncio
async def test_missing_schedule_params_mentions_after(cron_tool):
    """Error when no schedule param is given should mention 'after'."""
    result = await cron_tool.execute(action="add", message="no schedule")
    assert "Error" in result
    assert "after" in result


def test_schema_includes_after_property(cron_tool):
    """The tool schema should include the 'after' property."""
    schema = cron_tool.parameters
    assert "after" in schema["properties"]
    after_schema = schema["properties"]["after"]
    assert after_schema["type"] == "integer"
    assert after_schema["minimum"] == 10
