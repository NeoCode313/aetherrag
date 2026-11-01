"""Hybrid retrieval fusion via Reciprocal Rank Fusion (RRF).

Why RRF instead of a linear combination of raw scores?
* Cosine similarity lives in [-1, 1] and its empirical distribution depends on the
  embedding model's anisotropy.
* Sparse dot-product scores depend on lexical encoding and its normalization.
Neither score is a calibrated probability.  Reciprocal Rank Fusion (Cormack et al., 2009)
combines rank positions non-parametrically:

    RRF(d) = w_dense / (k + rank_dense(d)) + w_sparse / (k + rank_sparse(d))

We also add a deterministic exact-code boost when the query contains technical
identifiers (e.g. ``HX-900``, ``E-409``, ``ISO-10816``, ``PT-204``) that appear verbatim
in a candidate chunk.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from app.domain.entities import Chunk, ScoredChunk

TECHNICAL_CODE_RE = re.compile(
    # Use the same identifier boundaries as grounding checks. A word boundary
    # alone misclassifies the m3 in m³/s (NFKC: m3/s) as an equipment code.
    r"(?<![\w/-])(?:[A-Z][A-Z0-9]{0,9}(?:[-/][A-Z0-9]{1,12})*[-/]\d[A-Z0-9-]*|"
    r"[A-Z]{1,6}\d{1,6}|ISO\s+\d+(?:-\d+)?)(?![\w/-])", re.I)


def extract_technical_codes(query: str) -> list[str]:
    return [m.group(0).lower() for m in TECHNICAL_CODE_RE.finditer(normalize_technical_text(query))]


def normalize_technical_text(text: str) -> str:
    """Normalize typographic code separators without changing source text or offsets."""
    text = unicodedata.normalize("NFKC", text).replace("–", "-").replace("−", "-")
    return re.sub(r"(?<=[A-Za-z0-9])\s*([-/])\s*(?=[A-Za-z0-9])", r"\1", text)


class HybridRanker:
    def __init__(
        self,
        rrf_k: int = 60,
        dense_weight: float = 0.65,
        sparse_weight: float = 0.35,
        exact_code_boost: float = 0.006,
    ) -> None:
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        self.rrf_k = rrf_k
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight
        self.exact_code_boost = exact_code_boost

    def fuse(
        self,
        dense_results: Sequence[tuple[Chunk, float]],
        sparse_results: Sequence[tuple[Chunk, float]],
        query: str = "",
        top_k: int = 6,
    ) -> list[ScoredChunk]:
        by_id: dict[str, ScoredChunk] = {}

        for rank, (chunk, score) in enumerate(dense_results, start=1):
            rrf = self.dense_weight / (self.rrf_k + rank)
            by_id[chunk.chunk_id] = ScoredChunk(
                chunk=chunk,
                score=rrf,
                dense_score=float(score),
                dense_rank=rank,
                sparse_rank=None,
            )

        for rank, (chunk, sparse_score) in enumerate(sparse_results, start=1):
            rrf = self.sparse_weight / (self.rrf_k + rank)
            existing = by_id.get(chunk.chunk_id)
            if existing is None:
                by_id[chunk.chunk_id] = ScoredChunk(
                    chunk=chunk,
                    score=rrf,
                    dense_score=None,
                    sparse_score=round(float(sparse_score), 4),
                    dense_rank=None,
                    sparse_rank=rank,
                )
            else:
                existing.score += rrf
                existing.sparse_score = round(float(sparse_score), 4)
                existing.sparse_rank = rank

        codes = extract_technical_codes(query)
        if codes:
            for item in by_id.values():
                haystack = normalize_technical_text(item.chunk.text).lower()
                matches = sum(1 for code in codes if re.search(
                    r"(?<![\w/-])" + re.escape(code) + r"(?![\w/-])", haystack))
                if matches:
                    item.score += matches * self.exact_code_boost

        ranked = sorted(
            by_id.values(),
            key=lambda item: (
                item.score,
                item.dense_score if item.dense_score is not None else -1.0,
            ),
            reverse=True,
        )
        for item in ranked:
            item.score = round(item.score, 6)
        return ranked[:top_k]
