"""Tests for recall provisioning (sqlite-vec resolution + model cache layout).

The actual model download is network-bound and validated manually; these tests
cover the fast, deterministic pieces.
"""

from pathlib import Path

from ragnarbot.agent.index import EMBED_DIM, QUANT_FILES, provision


def test_sqlite_vec_supported_on_this_interpreter():
    # The project requires 3.11+, whose sqlite3 supports load_extension.
    assert provision.sqlite_vec_supported() is True


def test_ensure_sqlite_vec_returns_existing_binary():
    path = provision.ensure_sqlite_vec()
    assert path is not None
    # loadable_path returns the extension stem (no suffix); a sibling binary exists.
    p = Path(path)
    assert p.parent.is_dir()


def test_sqlite_vec_actually_loads_and_queries(tmp_path):
    import sqlite3

    import sqlite_vec

    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    (version,) = con.execute("select vec_version()").fetchone()
    assert isinstance(version, str) and version
    con.close()


def test_model_dir_is_versioned_per_quant_and_rev(tmp_path):
    d1 = provision.model_dir(tmp_path, "q4", "abcdef123456789")
    d2 = provision.model_dir(tmp_path, "q8", "abcdef123456789")
    d3 = provision.model_dir(tmp_path, "q4", "fedcba987654321")
    assert d1 != d2 and d1 != d3
    assert "q4-abcdef123456" in d1.name


def test_remote_files_includes_onnx_data_and_tokenizer():
    files = provision._remote_files("q4")
    assert QUANT_FILES["q4"] in files
    assert QUANT_FILES["q4"] + "_data" in files
    assert "tokenizer.json" in files
    assert "config.json" in files


def test_onnx_path_basename(tmp_path):
    assert provision.onnx_path(tmp_path, "q4").name == "model_q4.onnx"


def test_ensure_embedding_model_no_download_returns_none(tmp_path):
    # No marker present and download disabled -> None (graceful).
    import asyncio

    result = asyncio.run(
        provision.ensure_embedding_model(tmp_path, "q4", "deadbeef", allow_download=False)
    )
    assert result is None


def test_embed_dim_constant():
    assert EMBED_DIM == 768
