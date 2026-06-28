"""EmbeddingGemma-300m embedder on ONNX Runtime (CPU).

The pinned export emits a pooled ``sentence_embedding`` output (masked-mean-pool +
both dense projection layers baked in), so embedding is: run the session, take that
output, L2-normalize — no external pooling. Asymmetric task prefixes are applied at
embed time only (never stored). Query and batch-index inference run on separate
single-thread executors so a user query never queues behind a backfill batch.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ragnarbot.agent.index import DOC_PREFIX, EMBED_DIM, QUERY_PREFIX
from ragnarbot.agent.index.provision import onnx_path

# Hard ceiling for any single input fed to the model (model context is 2048;
# chunks are <=512 by construction, but guard against pathological queries).
_TRUNCATE_TOKENS = 2048


class Embedder:
    """Wraps an ONNX session + tokenizer; produces L2-normalized 768-d vectors."""

    def __init__(self, model_dir: Path, quant: str = "q4"):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._session = ort.InferenceSession(
            str(onnx_path(model_dir, quant)), providers=["CPUExecutionProvider"]
        )
        out_names = [o.name for o in self._session.get_outputs()]
        if "sentence_embedding" not in out_names:
            raise RuntimeError("ONNX export lacks 'sentence_embedding' output")
        self._in_names = {i.name for i in self._session.get_inputs()}

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=_TRUNCATE_TOKENS)

        self._query_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recall-embed-q")
        self._index_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recall-embed-i")

    # ── token counting (shared with the chunker for budget parity) ──
    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    # ── sync embedding ──────────────────────────────────────────────
    def _run(self, texts: list[str]) -> np.ndarray:
        encs = self.tokenizer.encode_batch(texts)
        maxlen = max((len(e.ids) for e in encs), default=1)
        ids = np.zeros((len(encs), maxlen), dtype=np.int64)
        mask = np.zeros((len(encs), maxlen), dtype=np.int64)
        for i, e in enumerate(encs):
            n = len(e.ids)
            ids[i, :n] = e.ids
            mask[i, :n] = 1

        feed: dict = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._in_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        if "position_ids" in self._in_names:
            feed["position_ids"] = np.clip(np.cumsum(mask, axis=1) - 1, 0, None).astype(np.int64)

        out = self._session.run(["sentence_embedding"], feed)[0].astype(np.float32)
        norms = np.clip(np.linalg.norm(out, axis=1, keepdims=True), 1e-9, None)
        return out / norms

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        return self._run([DOC_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._run([QUERY_PREFIX + text])[0]

    # ── async wrappers on dedicated lanes ───────────────────────────
    async def aembed_documents(self, texts: list[str]) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._index_pool, self.embed_documents, texts)

    async def aembed_query(self, text: str) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._query_pool, self.embed_query, text)

    def close(self) -> None:
        self._query_pool.shutdown(wait=False, cancel_futures=True)
        self._index_pool.shutdown(wait=False, cancel_futures=True)
