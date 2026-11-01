"""Pinned multilingual E5 inference on CPU; no remote code or document egress."""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading

import numpy as np

from app.domain.exceptions import ValidationError

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
FILES = {
    "model.onnx": ("onnx/model_qint8_avx512_vnni.onnx", "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
    "tokenizer.json": ("onnx/tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),  # Public artifact SHA-256. gitleaks:allow
}


def verify_model(directory: Path):
    for name, (_, expected) in FILES.items():
        path = directory / name
        if not path.is_file():
            raise ValidationError("Falta el modelo semántico local. Ejecuta download_semantic_model.py antes del arranque.")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValidationError("El modelo semántico no coincide con el checksum fijado")


class MultilingualE5EmbeddingProvider:
    dimension = 384
    model_name = MODEL

    def __init__(self, directory: Path, *, threads: int = 2):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        verify_model(directory)
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(threads, 4))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self._lock = threading.RLock()

    def count_tokens(self, text):
        with self._lock:
            return len(self.tokenizer.encode(text).ids)

    def _encode(self, texts, prefix):
        if not texts:
            return []
        with self._lock:
            encodings = self.tokenizer.encode_batch([prefix + text for text in texts])
        if any(len(item.ids) > 512 for item in encodings):
            raise ValidationError("El texto supera el contexto del embedder E5; debe dividirse antes de indexar")
        inputs = {"input_ids": np.asarray([item.ids for item in encodings], dtype=np.int64),
                  "attention_mask": np.asarray([item.attention_mask for item in encodings], dtype=np.int64)}
        if "token_type_ids" in {item.name for item in self.session.get_inputs()}:
            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
        hidden = self.session.run(None, inputs)[0]
        mask = inputs["attention_mask"][..., None]
        vectors = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        if not np.isfinite(vectors).all() or vectors.shape[1] != self.dimension:
            raise ValidationError("El embedder local devolvió vectores incompatibles")
        return vectors.astype(np.float32).tolist()

    def embed_documents(self, texts):
        # Bound inference RAM independently of the ingestion batch size.
        return [vector for start in range(0, len(texts), 8)
                for vector in self._encode(texts[start:start+8], "passage: ")]

    def embed_query(self, text):
        return self._encode([text], "query: ")[0]
