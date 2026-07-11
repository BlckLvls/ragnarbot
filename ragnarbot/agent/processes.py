"""Shared subprocess lifecycle helpers for shell-backed tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any


def isolated_process_kwargs() -> dict[str, Any]:
    """Return platform-specific flags that isolate a subprocess tree."""
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


async def _wait_for_process(process: asyncio.subprocess.Process, timeout: float) -> bool:
    if process.returncode is not None:
        return True
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return True


def _signal_process_group(pid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_group(pid: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_group_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _terminate_windows_tree(process: asyncio.subprocess.Process, timeout: float) -> None:
    """Best-effort tree termination on Windows via the built-in taskkill utility."""
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=timeout)
    except (FileNotFoundError, ProcessLookupError, asyncio.TimeoutError):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    await _wait_for_process(process, timeout)


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float = 1.0,
) -> None:
    """Terminate a subprocess and every descendant, then reap the leader.

    Shell tools spawn each command in its own process group/session. Signalling
    only the shell leaves children such as ``npm``, ``npx``, and backgrounded
    scripts alive after a timeout, so POSIX cleanup targets the entire group.
    """
    grace_period = max(grace_period, 0.1)

    if os.name == "nt":
        await _terminate_windows_tree(process, grace_period)
        return

    if os.name != "posix":
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        if not await _wait_for_process(process, grace_period):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await _wait_for_process(process, grace_period)
        return

    pid = process.pid
    _signal_process_group(pid, signal.SIGTERM)
    leader_done = await _wait_for_process(process, grace_period)
    group_done = await _wait_for_process_group(pid, grace_period)
    if leader_done and group_done:
        return

    _signal_process_group(pid, signal.SIGKILL)
    await _wait_for_process(process, grace_period)
    await _wait_for_process_group(pid, grace_period)
