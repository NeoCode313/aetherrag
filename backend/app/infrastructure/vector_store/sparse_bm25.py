"""Deterministic hashed lexical vectors with saturated TF and L2 normalization.

No corpus IDF, document-length normalization or learned semantic representation.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter

from app.application.services.hybrid_ranker import normalize_technical_text
from app.domain.entities import SparseVector
from app.domain.ports import SparseEncoder

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-/.][a-z0-9]+)*", re.IGNORECASE)
CODE_TOKEN_RE = re.compile(r"^(?:[a-z]+\d+|\d+[a-z]+|[a-z0-9]+-[a-z0-9-]+)$", re.IGNORECASE)

STOPWORDS = frozenset(
    {
        "de", "la", "el", "en", "y", "a", "los", "del", "se", "las", "por", "un", "para",
        "con", "no", "una", "su", "al", "lo", "como", "mas", "pero", "sus", "le", "ya",
        "o", "este", "si", "porque", "esta", "entre", "cuando", "muy", "sin", "sobre",
        "tambien", "me", "hasta", "hay", "donde", "quien", "desde", "todo", "nos",
        "the", "and", "of", "to", "in", "is", "for", "on", "that", "with", "as", "by",
        "at", "from", "be", "this", "are", "or", "an", "que", "cual", "cuales", "como",
    }
)


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


def _bucket(token: str, num_buckets: int) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little") % num_buckets


class HashedLexicalEncoder(SparseEncoder):
    """Stateless TF-saturation lexical encoder; no corpus IDF or BM25 length normalization."""

    def __init__(self, num_buckets: int = 262_144, k1: float = 1.5) -> None:
        self.num_buckets = num_buckets
        self.k1 = k1

    def _tokenize(self, text: str) -> list[tuple[str, float]]:
        normalized = _strip_accents(normalize_technical_text(text).lower())
        raw_tokens = TOKEN_RE.findall(normalized)
        weighted: list[tuple[str, float]] = []

        filtered: list[str] = []
        for tok in raw_tokens:
            if len(tok) < 2 or tok in STOPWORDS:
                continue
            filtered.append(tok)
            is_code = bool(CODE_TOKEN_RE.match(tok))
            boost = 2.4 if is_code else 1.0
            weighted.append((tok, boost))
            # Also index split parts of hyphenated codes (e.g. "hx-900" -> "hx", "900")
            if "-" in tok or "/" in tok:
                for sub in re.split(r"[-/]", tok):
                    if len(sub) >= 2 and sub not in STOPWORDS:
                        weighted.append((sub, 1.3))

        # Add word bigrams for phrase precision
        for a, b in zip(filtered, filtered[1:]):
            weighted.append((f"{a}_{b}", 1.15))

        return weighted

    def encode_document(self, text: str) -> SparseVector:
        tokens = self._tokenize(text)
        if not tokens:
            return SparseVector(indices=[0], values=[0.0])

        tf_map: Counter[int] = Counter()
        boost_map: dict[int, float] = {}
        for tok, boost in tokens:
            idx = _bucket(tok, self.num_buckets)
            tf_map[idx] += 1
            boost_map[idx] = max(boost_map.get(idx, 1.0), boost)

        indices = sorted(tf_map.keys())
        values: list[float] = []
        for idx in indices:
            tf = tf_map[idx]
            bm25_tf = (tf * (self.k1 + 1.0)) / (tf + self.k1)
            values.append(round(bm25_tf * boost_map[idx], 5))

        # L2 normalize for stable dot products
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        values = [round(v / norm, 5) for v in values]
        return SparseVector(indices=indices, values=values)

    def encode_query(self, text: str) -> SparseVector:
        return self.encode_document(text)


# Compatibility import; retained for existing DI. This encoder is not conventional BM25.
BM25SparseEncoder = HashedLexicalEncoder
