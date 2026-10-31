"""Ports (hexagonal architecture): the contracts the application core depends on.

Concrete adapters live in ``app.infrastructure`` and are wired by the DI container.
Swapping Gemini -> Ollama -> OpenAI, or Qdrant -> another vector DB, means writing a
new adapter that satisfies one of these Protocols; the use cases do not change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, Any
from typing import Protocol, runtime_checkable

from app.domain.entities import (
    Chunk,
    Document,
    GeneratedAnswer,
    ImageInput,
    IngestionJob,
    ParsedDocument,
    SparseVector,
)

ProgressCallback = Callable[[float, str], None]


@runtime_checkable
class DocumentParser(Protocol):
    def parse(
        self, pdf_path: Path, document_id: str, file_name: str, on_progress: ProgressCallback | None = None
    ) -> ParsedDocument: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_answer(
        self, system_instruction: str, prompt: str, images: Sequence[ImageInput] = ()
    ) -> GeneratedAnswer: ...

    def describe_image(self, image: ImageInput, context: str) -> str: ...


@runtime_checkable
class SparseEncoder(Protocol):
    def encode_document(self, text: str) -> SparseVector: ...

    def encode_query(self, text: str) -> SparseVector: ...


@runtime_checkable
class VectorStore(Protocol):
    def ensure_collection(self, dimension: int) -> None: ...

    def upsert(
        self, chunks: Sequence[Chunk], dense: Sequence[list[float]], sparse: Sequence[SparseVector]
    ) -> None: ...

    def dense_search(
        self, vector: list[float], limit: int, document_ids: Sequence[str] | None = None
    ) -> list[tuple[Chunk, float]]: ...

    def sparse_search(
        self, vector: SparseVector, limit: int, document_ids: Sequence[str] | None = None
    ) -> list[tuple[Chunk, float]]: ...

    def delete_document(self, document_id: str) -> None: ...

    def healthy(self) -> bool: ...


@runtime_checkable
class JobRepository(Protocol):
    def save(self, job: IngestionJob) -> None: ...

    def get(self, job_id: str) -> IngestionJob | None: ...


@runtime_checkable
class DocumentRepository(Protocol):
    def save(self, document: Document) -> None: ...

    def get(self, document_id: str) -> Document | None: ...

    def find_by_hash(self, sha256: str) -> Document | None: ...

    def list(self) -> list[Document]: ...

    def delete(self, document_id: str) -> None: ...


@runtime_checkable
class FileStorage(Protocol):
    def save_stream(self, stream: BinaryIO, max_bytes: int) -> tuple[str, str, int]: ...

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path: ...

    def save_upload(self, document_id: str, data: bytes) -> Path: ...

    def upload_path(self, document_id: str) -> Path: ...

    def assets_dir(self, document_id: str) -> Path: ...

    def asset_path(self, document_id: str, file_name: str) -> Path: ...

    def page_image_path(self, document_id: str, page: int) -> Path: ...

    def delete_document(self, document_id: str) -> None: ...


@runtime_checkable
class TaskQueue(Protocol):
    def enqueue_ingestion(self, job_id: str) -> None: ...
