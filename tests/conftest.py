"""Shared pytest fixtures."""

import pytest

from ragnarbot.agent.tools import ripgrep


@pytest.fixture(autouse=True)
def _block_ripgrep_download(monkeypatch):
    """Never download ripgrep over the network during tests.

    Provisioning logic is unit-tested with mocks in test_ripgrep_provision.py;
    everywhere else, a blocked download just degrades to the Python search
    fallback (ensure_ripgrep swallows the error and returns None), so no test
    hits the network or writes a binary into the real profile data root.
    """
    async def _blocked(asset, dest):
        raise RuntimeError("ripgrep download disabled during tests")

    monkeypatch.setattr(ripgrep, "_download_and_extract", _blocked)
