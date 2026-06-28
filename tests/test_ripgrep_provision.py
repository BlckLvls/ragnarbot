"""Tests for lazy ripgrep provisioning (no network)."""

import stat

import pytest

from ragnarbot.agent.tools import ripgrep


def test_asset_name_known_platforms(monkeypatch):
    monkeypatch.setattr(ripgrep.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ripgrep.platform, "machine", lambda: "arm64")
    assert ripgrep.asset_name() == f"ripgrep-{ripgrep.RIPGREP_VERSION}-aarch64-apple-darwin.tar.gz"

    monkeypatch.setattr(ripgrep.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ripgrep.platform, "machine", lambda: "x86_64")
    assert ripgrep.asset_name() == f"ripgrep-{ripgrep.RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz"


def test_asset_name_unsupported_platform(monkeypatch):
    monkeypatch.setattr(ripgrep.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(ripgrep.platform, "machine", lambda: "pdp11")
    assert ripgrep.asset_name() is None


def test_managed_rg_path_is_versioned(tmp_path):
    p = ripgrep.managed_rg_path(tmp_path)
    assert ripgrep.RIPGREP_VERSION in str(p)
    assert p.name in ("rg", "rg.exe")


@pytest.mark.asyncio
async def test_ensure_returns_system_rg(tmp_path, monkeypatch):
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: "/usr/local/bin/rg")
    result = await ripgrep.ensure_ripgrep(tmp_path)
    assert result == "/usr/local/bin/rg"


@pytest.mark.asyncio
async def test_ensure_no_download_when_disallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: None)
    result = await ripgrep.ensure_ripgrep(tmp_path, allow_download=False)
    assert result is None


@pytest.mark.asyncio
async def test_ensure_returns_cached_managed_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: None)
    dest = ripgrep.managed_rg_path(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("#!/bin/sh\n", encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR)
    # Should return the cached binary without attempting any download.
    result = await ripgrep.ensure_ripgrep(tmp_path, allow_download=True)
    assert result == str(dest)


@pytest.mark.asyncio
async def test_ensure_unsupported_platform_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: None)
    monkeypatch.setattr(ripgrep.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(ripgrep.platform, "machine", lambda: "pdp11")
    # No asset for this platform → returns None without any network call.
    result = await ripgrep.ensure_ripgrep(tmp_path, allow_download=True)
    assert result is None


@pytest.mark.asyncio
async def test_ensure_none_data_root(monkeypatch):
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: None)
    assert await ripgrep.ensure_ripgrep(None) is None


def test_find_member():
    names = ["ripgrep-14.1.1-aarch64-apple-darwin/doc/rg.1", "ripgrep-14.1.1-aarch64-apple-darwin/rg"]
    assert ripgrep._find_member(names, "rg") == "ripgrep-14.1.1-aarch64-apple-darwin/rg"
    assert ripgrep._find_member(names, "rg.exe") is None


@pytest.mark.asyncio
async def test_ensure_download_failure_returns_none(tmp_path, monkeypatch):
    """A failed download must degrade to None (Python fallback), not raise."""
    monkeypatch.setattr(ripgrep.shutil, "which", lambda _: None)

    async def _boom(asset, dest):
        raise RuntimeError("network down")

    monkeypatch.setattr(ripgrep, "_download_and_extract", _boom)
    result = await ripgrep.ensure_ripgrep(tmp_path, allow_download=True)
    assert result is None


def test_extract_binary_from_tar(tmp_path):
    """Extract rg from a tar.gz the way a real release is laid out."""
    import io
    import tarfile

    archive = tmp_path / "rg.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = b"#!/bin/sh\necho ripgrep\n"
        info = tarfile.TarInfo("ripgrep-14.1.1-x/rg")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    out = tmp_path / "rg"
    ripgrep._extract_binary(archive, out, is_zip=False)
    assert out.read_bytes().startswith(b"#!/bin/sh")
