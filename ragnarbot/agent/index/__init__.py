"""Hybrid (dense vector + BM25) recall index over memory files and chats.

Local, zero-touch: EmbeddingGemma-300m (q4 ONNX) runs on ONNX Runtime CPU and the
sqlite-vec extension + model auto-download on first use. Indexing runs in the
background on the same triggers as the memory-flush subsystem.

This package is intentionally import-light at module load: heavy deps (onnxruntime,
the model) are touched only when the embedder/store are actually constructed, so a
profile that never uses recall pays nothing at import time.
"""

# ── model / embedding constants ─────────────────────────────────────
EMBED_DIM = 768  # full Matryoshka dim; changing it invalidates all vectors (part of model_key)
MODEL_REPO = "onnx-community/embeddinggemma-300m-ONNX"

# quant -> ONNX filename inside the repo. q4 is the measured default (lowest RSS).
QUANT_FILES = {
    "q4": "onnx/model_q4.onnx",
    "q8": "onnx/model_quantized.onnx",  # int8 dynamic — heavier on CPU, kept selectable
    "fp16": "onnx/model_fp16.onnx",
    "fp32": "onnx/model.onnx",
}
# external-weights sidecar that always accompanies the .onnx for this export
ONNX_DATA_SUFFIX = "_data"
TOKENIZER_FILES = ("tokenizer.json", "config.json")

# Asymmetric task prefixes (required for EmbeddingGemma retrieval quality).
DOC_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "

# ── chunking constants ──────────────────────────────────────────────
MAX_TOKENS = 512          # hard cap per chunk (model context budget for a unit)
TARGET_TOKENS = 400       # preferred chunk size
OVERLAP_TOKENS = 60       # overlap carried between adjacent chunks
MIN_CHUNK_TOKENS = 32     # floor; smaller tails get merged
MAX_TURNS = 6             # max conversation turns packed into one chat chunk
SPECIAL_HEADROOM = 4      # budget slack for BOS/EOS the embedder adds vs the chunker count


def model_key(quant: str) -> str:
    """Identity of the embedding space; bumped invalidates cached vectors/chunks."""
    return f"embeddinggemma-300m/{quant}/{EMBED_DIM}"
