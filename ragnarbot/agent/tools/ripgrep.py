"""Lazy provisioning of the official ripgrep binary.

When ripgrep is not on PATH, the grep tool can download the official
prebuilt binary from the BurntSushi/ripgrep releases into the profile's
data root on first use (mirroring how the browser tool installs Chromium).
Resolution is best-effort: any failure returns None so grep falls back to
its pure-Python backend instead of erroring.
"""

import asyncio
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx
from loguru import logger

RIPGREP_VERSION = "14.1.1"

# (platform.system(), platform.machine()) -> release asset filename template
_ASSETS = {
    ("Darwin", "arm64"): "ripgrep-{v}-aarch64-apple-darwin.tar.gz",
    ("Darwin", "x86_64"): "ripgrep-{v}-x86_64-apple-darwin.tar.gz",
    ("Linux", "x86_64"): "ripgrep-{v}-x86_64-unknown-linux-musl.tar.gz",
    ("Linux", "aarch64"): "ripgrep-{v}-aarch64-unknown-linux-gnu.tar.gz",
    ("Windows", "AMD64"): "ripgrep-{v}-x86_64-pc-windows-msvc.zip",
}
_DOWNLOAD_URL = "https://github.com/BurntSushi/ripgrep/releases/download/{v}/{asset}"

_download_lock = asyncio.Lock()


def asset_name() -> str | None:
    """Return the release asset filename for the current platform, or None."""
    template = _ASSETS.get((platform.system(), platform.machine()))
    return template.format(v=RIPGREP_VERSION) if template else None


def managed_rg_path(data_root: Path) -> Path:
    """Path where the downloaded rg binary is cached, versioned per release."""
    exe = "rg.exe" if platform.system() == "Windows" else "rg"
    return Path(data_root) / "tools" / "ripgrep" / RIPGREP_VERSION / exe


async def ensure_ripgrep(data_root: Path | None, *, allow_download: bool = True) -> str | None:
    """Return a path to a working rg binary, or None if unavailable.

    Resolution order: system rg on PATH → previously downloaded managed
    binary → fresh download (when allowed). Never raises.
    """
    sys_rg = shutil.which("rg")
    if sys_rg:
        return sys_rg
    if data_root is None:
        return None

    dest = managed_rg_path(data_root)
    if dest.exists() and os.access(dest, os.X_OK):
        return str(dest)
    if not allow_download:
        return None

    asset = asset_name()
    if not asset:
        logger.debug("ripgrep auto-install: unsupported platform {}", platform.platform())
        return None

    async with _download_lock:
        # Re-check after acquiring the lock (another task may have finished).
        if dest.exists() and os.access(dest, os.X_OK):
            return str(dest)
        try:
            logger.info("Installing ripgrep {} (first-time setup)...", RIPGREP_VERSION)
            await _download_and_extract(asset, dest)
            logger.info("ripgrep installed at {}", dest)
            return str(dest)
        except Exception as exc:
            logger.warning(
                "ripgrep auto-install failed ({}); using the Python search fallback", exc
            )
            return None


async def _download_and_extract(asset: str, dest: Path) -> None:
    url = _DOWNLOAD_URL.format(v=RIPGREP_VERSION, asset=asset)
    is_zip = asset.endswith(".zip")

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content

    def _extract() -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stage in a temp dir on the SAME filesystem so os.replace is atomic.
        with tempfile.TemporaryDirectory(dir=str(dest.parent)) as td:
            archive = Path(td) / ("rg.zip" if is_zip else "rg.tar.gz")
            archive.write_bytes(data)
            staged = Path(td) / dest.name
            _extract_binary(archive, staged, is_zip)
            staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(staged, dest)

    await asyncio.to_thread(_extract)


def _extract_binary(archive: Path, dest_bin: Path, is_zip: bool) -> None:
    target = "rg.exe" if is_zip else "rg"
    if is_zip:
        with zipfile.ZipFile(archive) as zf:
            member = _find_member(zf.namelist(), target)
            if not member:
                raise RuntimeError(f"{target} not found in ripgrep archive")
            with zf.open(member) as src, dest_bin.open("wb") as out:
                shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            member = _find_member(tf.getnames(), target)
            if not member:
                raise RuntimeError(f"{target} not found in ripgrep archive")
            extracted = tf.extractfile(member)
            if extracted is None:
                raise RuntimeError("could not read rg from ripgrep archive")
            with extracted as src, dest_bin.open("wb") as out:
                shutil.copyfileobj(src, out)


def _find_member(names: list[str], target: str) -> str | None:
    for n in names:
        if n.rsplit("/", 1)[-1] == target:
            return n
    return None
