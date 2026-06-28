"""Zero-touch provisioning for the recall index.

Two runtime dependencies:

* **sqlite-vec extension** — ships inside the ``sqlite-vec`` pip wheel (a base
  dependency) which bundles the platform-specific ``vec0`` binary, so there is
  nothing to download; we only resolve its path and verify the interpreter can
  load extensions at all (the macOS *system* Python 3.9 cannot — the project's
  3.11+ interpreters can).
* **EmbeddingGemma q4 ONNX model** — downloaded once from the ungated
  ``onnx-community`` mirror, mirroring ``agent/tools/ripgrep.py``: a module-level
  asyncio lock, double-checked, staged on the same filesystem then atomically
  moved, verified before a ``.ready`` marker is written, and never raising
  (returns ``None`` so the caller disables recall gracefully).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import httpx
from loguru import logger

from ragnarbot.agent.index import (
    DOC_PREFIX,
    EMBED_DIM,
    MODEL_REPO,
    ONNX_DATA_SUFFIX,
    QUANT_FILES,
    TOKENIZER_FILES,
)

DEFAULT_REV = "5090578d9565bb06545b4552f76e6bc2c93e4a66"
_HF_URL = "https://huggingface.co/{repo}/resolve/{rev}/{file}"
_model_lock = asyncio.Lock()


# ── sqlite-vec ──────────────────────────────────────────────────────

def sqlite_vec_supported() -> bool:
    """Return True if this interpreter's sqlite3 can load extensions.

    sqlite-vec is loaded as a runtime extension, which requires
    ``Connection.enable_load_extension`` — present on the project's uv/Homebrew
    CPython 3.11+, absent on the macOS system Python. Never raises.
    """
    con = None
    try:
        con = sqlite3.connect(":memory:")
        if not hasattr(con, "enable_load_extension"):
            return False
        con.enable_load_extension(True)
        return True
    except Exception:
        return False
    finally:
        if con is not None:
            con.close()


def ensure_sqlite_vec() -> str | None:
    """Resolve the bundled sqlite-vec loadable extension path, or None."""
    try:
        import sqlite_vec
    except Exception as exc:  # pragma: no cover - packaging error
        logger.warning("sqlite-vec not importable ({}); recall disabled", exc)
        return None
    try:
        return sqlite_vec.loadable_path()
    except Exception as exc:  # pragma: no cover - missing platform binary
        logger.warning("sqlite-vec loadable_path unavailable ({}); recall disabled", exc)
        return None


# ── embedding model ─────────────────────────────────────────────────

def model_dir(models_root: Path, quant: str, rev: str) -> Path:
    """Versioned cache dir for a (quant, rev) model build."""
    return Path(models_root) / "embeddinggemma-300m" / f"{quant}-{rev[:12]}"


def _remote_files(quant: str) -> list[str]:
    if quant not in QUANT_FILES:
        raise ValueError(f"unknown quant {quant!r}")
    onnx = QUANT_FILES[quant]
    return [onnx, onnx + ONNX_DATA_SUFFIX, *TOKENIZER_FILES]


def onnx_path(dest: Path, quant: str) -> Path:
    """Path to the model .onnx file inside a provisioned model dir."""
    return dest / Path(QUANT_FILES[quant]).name


async def ensure_embedding_model(
    models_root: Path,
    quant: str = "q4",
    rev: str = DEFAULT_REV,
    *,
    allow_download: bool = True,
) -> Path | None:
    """Return a ready model dir (downloading once if needed), or None.

    The dir contains ``model_<quant>.onnx`` (+ its ``.onnx_data`` sidecar) and
    ``tokenizer.json`` / ``config.json``. Never raises.
    """
    dest = model_dir(models_root, quant, rev)
    marker = dest / ".ready"
    if marker.exists():
        return dest
    if not allow_download:
        return None

    async with _model_lock:
        if marker.exists():  # another task finished while we waited
            return dest
        try:
            await _download_model(quant, rev, dest)
            ok = await asyncio.to_thread(_verify_model, dest, quant)
            if not ok:
                shutil.rmtree(dest, ignore_errors=True)
                return None
            marker.write_text(rev)
            logger.info("EmbeddingGemma {} model ready at {}", quant, dest)
            return dest
        except Exception as exc:
            logger.warning("embedding model provisioning failed ({}); recall disabled", exc)
            shutil.rmtree(dest, ignore_errors=True)
            return None


async def _download_model(quant: str, rev: str, dest: Path) -> None:
    """Download the model files into ``dest`` atomically (staged sibling dir)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    staging = Path(tempfile.mkdtemp(dir=str(dest.parent), prefix=dest.name + ".incomplete-"))
    try:
        logger.info("Downloading EmbeddingGemma {} model (first-time setup)...", quant)
        async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
            for remote in _remote_files(quant):
                url = _HF_URL.format(repo=MODEL_REPO, rev=rev, file=remote)
                out = staging / Path(remote).name
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with out.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            fh.write(chunk)
        os.replace(staging, dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _verify_model(dest: Path, quant: str) -> bool:
    """Load the ONNX in ORT and run a 1-token inference before trusting the cache.

    Confirms the export has the pooled ``sentence_embedding`` output at the
    expected dimension — a renamed/empty/wrong asset cannot poison the cache.
    """
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    path = onnx_path(dest, quant)
    tok = Tokenizer.from_file(str(dest / "tokenizer.json"))
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])

    out_names = [o.name for o in sess.get_outputs()]
    if "sentence_embedding" not in out_names:
        logger.warning("ONNX export lacks 'sentence_embedding' output; unusable")
        return False

    in_names = [i.name for i in sess.get_inputs()]
    enc = tok.encode(DOC_PREFIX + "test")
    ids = np.array([enc.ids], dtype=np.int64)
    mask = np.array([[1] * len(enc.ids)], dtype=np.int64)
    feed: dict = {}
    if "input_ids" in in_names:
        feed["input_ids"] = ids
    if "attention_mask" in in_names:
        feed["attention_mask"] = mask
    if "token_type_ids" in in_names:
        feed["token_type_ids"] = np.zeros_like(ids)
    if "position_ids" in in_names:
        feed["position_ids"] = np.clip(np.cumsum(mask, axis=1) - 1, 0, None).astype(np.int64)

    outs = sess.run(["sentence_embedding"], feed)
    dim = int(np.asarray(outs[0]).shape[-1])
    if dim != EMBED_DIM:
        logger.warning("ONNX sentence_embedding dim {} != expected {}", dim, EMBED_DIM)
        return False
    return True
