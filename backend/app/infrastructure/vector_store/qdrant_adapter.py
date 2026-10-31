"""Qdrant vector store adapter supporting dense cosine search and hashed lexical dot products.

Supports three deployment modes seamlessly through ``qdrant-client``:
1. Remote Qdrant server (``url="http://qdrant:6333"`` in Docker Compose).
2. Local embedded Qdrant on disk (``path="./data/qdrant_local"``) when no external container
   is reachable.
3. Ephemeral in-memory Qdrant (``location=":memory:"``) for fast integration tests.
"""

from __future__ import annotations

import logging
import uuid
import copy
import threading
from contextlib import nullcontext
import httpx
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.domain.exceptions import ConflictError, DependencyUnavailableError, ProviderError
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.domain.entities import (
    BoundingBox,
    Chunk,
    ChunkKind,
    LinkedVisual,
    LinkReason,
    SparseVector,
    VisualAsset,
    VisualKind,
)
from app.domain.ports import VectorStore

logger = logging.getLogger(__name__)


def _chunk_to_payload(chunk: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "document_name": chunk.document_name,
        "page": chunk.page,
        "kind": chunk.kind.value,
        "text": chunk.text,
        "bbox": chunk.bbox.to_list(),
        "section_path": list(chunk.section_path),
        "has_visuals": bool(chunk.visuals),
        "storage_id": chunk.storage_id,
        "embedding_prefix": chunk.embedding_prefix,
        "visuals": [
            {
                "distance": lv.distance,
                "reason": lv.reason.value,
                "asset": {
                    "asset_id": lv.asset.asset_id,
                    "document_id": lv.asset.document_id,
                    "page": lv.asset.page,
                    "bbox": lv.asset.bbox.to_list(),
                    "kind": lv.asset.kind.value,
                    "file_name": lv.asset.file_name,
                    "caption": lv.asset.caption,
                    "figure_number": lv.asset.figure_number,
                    "figure_label": lv.asset.figure_label,
                    "inner_text": lv.asset.inner_text,
                    "description": lv.asset.description,
                    "width_px": lv.asset.width_px,
                    "height_px": lv.asset.height_px,
                },
            }
            for lv in chunk.visuals
        ],
    }


def _chunk_from_payload(payload: dict[str, Any]) -> Chunk:
    visuals: list[LinkedVisual] = []
    for item in payload.get("visuals", []):
        a = item["asset"]
        asset = VisualAsset(
            asset_id=a["asset_id"],
            document_id=a["document_id"],
            page=int(a["page"]),
            bbox=BoundingBox.from_list(a["bbox"]),
            kind=VisualKind(a["kind"]),
            file_name=a["file_name"],
            caption=a.get("caption"),
            figure_number=a.get("figure_number"),
            figure_label=a.get("figure_label"),
            inner_text=a.get("inner_text", ""),
            description=a.get("description"),
            width_px=int(a.get("width_px", 0)),
            height_px=int(a.get("height_px", 0)),
        )
        visuals.append(
            LinkedVisual(
                asset=asset,
                distance=float(item["distance"]) if item.get("distance") is not None else None,
                reason=LinkReason(item["reason"]),
            )
        )

    return Chunk(
        chunk_id=payload["chunk_id"],
        document_id=payload["document_id"],
        document_name=payload["document_name"],
        page=int(payload["page"]),
        kind=ChunkKind(payload["kind"]),
        text=payload["text"],
        bbox=BoundingBox.from_list(payload["bbox"]),
        section_path=tuple(payload.get("section_path", [])),
        visuals=visuals,
        storage_id=payload.get("storage_id"),
        embedding_prefix=payload.get("embedding_prefix"),
    )


class QdrantHybridVectorStore(VectorStore):
    """Production Qdrant adapter with named dense + sparse vectors."""

    DENSE_NAME = "dense"
    SPARSE_NAME = "sparse"

    def _call(self, operation, *args, **kwargs):
        """Normalize transport/5xx failures without retrying auth or invalid requests."""
        try:
            with self._local_lock if self.mode != "remote" else nullcontext():
                return operation(*args, **kwargs)
        except UnexpectedResponse as exc:
            if exc.status_code in (408, 429) or exc.status_code >= 500:
                raise DependencyUnavailableError("Qdrant no está disponible temporalmente") from exc
            raise ProviderError(f"Qdrant rechazó la operación (HTTP {exc.status_code})") from exc
        except ResponseHandlingException as exc:
            cause = exc.source
            if isinstance(cause, (httpx.TransportError, TimeoutError, ConnectionError, OSError)):
                raise DependencyUnavailableError("Falló el transporte hacia Qdrant") from exc
            raise ProviderError("Qdrant devolvió una respuesta inválida") from exc
        except (httpx.TransportError, TimeoutError, ConnectionError) as exc:
            raise DependencyUnavailableError("Falló la conexión con Qdrant") from exc

    def __init__(
        self,
        collection_name: str = "multimodal_technical_chunks",
        url: str | None = None,
        api_key: str | None = None,
        local_path: Path | None = None,
        in_memory: bool = False,
    ) -> None:
        self.collection_name = collection_name
        self.mode = "memory"
        self.run_id = None
        self.allowed_run_ids = None
        self._collection_lock = threading.RLock()
        self._local_lock = threading.RLock()
        if in_memory:
            self.client = QdrantClient(location=":memory:")
            self.mode = "memory"
        elif url:
            try:
                self.client = QdrantClient(url=url, api_key=api_key or None, timeout=10.0)
                self._call(self.client.get_collections)
                self.mode = "remote"
            except DependencyUnavailableError as exc:
                raise DependencyUnavailableError("Qdrant remoto no está disponible; no se cambia de backend") from exc
        elif local_path is not None:
            Path(local_path).mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(local_path))
            self.mode = f"embedded({local_path})"
        else:
            self.client = QdrantClient(location=":memory:")

    def ensure_collection(self, dimension: int) -> None:
        with self._collection_lock:
            self._ensure_collection(dimension)

    def _ensure_collection(self, dimension: int) -> None:
        existing = {c.name for c in self._call(self.client.get_collections).collections}
        if self.collection_name in existing:
            info = self._call(self.client.get_collection, self.collection_name)
            vectors_cfg = info.config.params.vectors
            if isinstance(vectors_cfg, dict) and self.DENSE_NAME in vectors_cfg:
                if vectors_cfg[self.DENSE_NAME].size == dimension:
                    self._ensure_payload_indexes()
                    return
            raise ConflictError("La dimensión del índice es incompatible; cree una generación nueva")

        self._call(self.client.create_collection,
            collection_name=self.collection_name,
            vectors_config={
                self.DENSE_NAME: qmodels.VectorParams(
                    size=dimension,
                    distance=qmodels.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                self.SPARSE_NAME: qmodels.SparseVectorParams(
                    index=qmodels.SparseIndexParams(on_disk=False)
                )
            },
        )
        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        if self.mode != "remote":
            return
        info = self._call(self.client.get_collection, self.collection_name)
        for field in ("document_id", "run_id"):
            if field not in (info.payload_schema or {}):
                self._call(self.client.create_payload_index, self.collection_name, field,
                           field_schema=qmodels.PayloadSchemaType.KEYWORD, wait=True)

    def upsert(
        self,
        chunks: Sequence[Chunk],
        dense: Sequence[list[float]],
        sparse: Sequence[SparseVector],
    ) -> None:
        if not chunks:
            return
        points: list[qmodels.PointStruct] = []
        for ch, d_vec, s_vec in zip(chunks, dense, sparse, strict=True):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self.run_id or 'legacy'}:{ch.chunk_id}"))
            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector={
                        self.DENSE_NAME: list(d_vec),
                        self.SPARSE_NAME: qmodels.SparseVector(
                            indices=list(s_vec.indices),
                            values=list(s_vec.values),
                        ),
                    },
                    payload={**_chunk_to_payload(ch), "run_id": self.run_id},
                )
            )
        for start in range(0, len(points), 128):
            self._call(self.client.upsert, collection_name=self.collection_name, points=points[start:start + 128], wait=True)

    def view(self, collection_name: str, *, run_id: str | None = None,
             active_run_ids: Sequence[str] | None = None) -> QdrantHybridVectorStore:
        result = copy.copy(self)
        result.collection_name = collection_name
        result.run_id = run_id
        result.allowed_run_ids = None if active_run_ids is None else tuple(active_run_ids)
        return result

    def _build_filter(self, document_ids: Sequence[str] | None) -> qmodels.Filter | None:
        conditions = []
        if document_ids is not None:
            conditions.append(qmodels.FieldCondition(key="document_id", match=qmodels.MatchAny(any=list(document_ids))))
        if self.allowed_run_ids is not None:
            conditions.append(qmodels.FieldCondition(key="run_id", match=qmodels.MatchAny(any=list(self.allowed_run_ids))))
        return qmodels.Filter(must=conditions) if conditions else None

    def _collection_exists(self) -> bool:
        return self.collection_name in {c.name for c in self._call(self.client.get_collections).collections}

    def dense_search(
        self,
        vector: list[float],
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        if not self._collection_exists():
            return []
        response = self._call(self.client.query_points,
            collection_name=self.collection_name,
            query=list(vector),
            using=self.DENSE_NAME,
            query_filter=self._build_filter(document_ids),
            limit=limit,
            with_payload=True,
        )
        return [
            (_chunk_from_payload(pt.payload), float(pt.score))
            for pt in response.points
            if pt.payload is not None
        ]

    def sparse_search(
        self,
        vector: SparseVector,
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        if not self._collection_exists() or not vector.indices:
            return []
        response = self._call(self.client.query_points,
            collection_name=self.collection_name,
            query=qmodels.SparseVector(indices=list(vector.indices), values=list(vector.values)),
            using=self.SPARSE_NAME,
            query_filter=self._build_filter(document_ids),
            limit=limit,
            with_payload=True,
        )
        return [
            (_chunk_from_payload(pt.payload), float(pt.score))
            for pt in response.points
            if pt.payload is not None and float(pt.score) > 0.0
        ]

    def delete_document(self, document_id: str) -> None:
        if not self._collection_exists():
            return
        self._call(self.client.delete,
            collection_name=self.collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="document_id",
                            match=qmodels.MatchValue(value=document_id),
                        )
                    ]
                )
            ),
            wait=True,
        )

    def healthy(self) -> bool:
        try:
            self._call(self.client.get_collections)
            return True
        except Exception:
            return False

    def count_points(self) -> int:
        if not self._collection_exists():
            return 0
        return int(self._call(self.client.count, collection_name=self.collection_name,
            count_filter=self._build_filter(None), exact=True).count)

    def run_point_count(self, run_id: str) -> int:
        return self.view(self.collection_name, active_run_ids=[run_id]).count_points()

    def verify_run(self, run_id: str, expected_chunk_ids: Sequence[str]) -> bool:
        expected = set(expected_chunk_ids)
        if len(expected) != len(expected_chunk_ids):
            return False
        found = set()
        offset = None
        condition = qmodels.Filter(must=[qmodels.FieldCondition(
            key="run_id", match=qmodels.MatchValue(value=run_id))])
        while True:
            points, offset = self._call(self.client.scroll, self.collection_name, scroll_filter=condition,
                offset=offset, limit=128, with_payload=["chunk_id"], with_vectors=False)
            found.update(p.payload["chunk_id"] for p in points if p.payload)
            if offset is None:
                break
        return found == expected
