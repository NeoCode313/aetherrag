"""Filesystem storage and state repositories (File + optional Redis)."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.entities import Document, IngestionJob, JobStatus
from app.domain.ports import DocumentRepository, FileStorage, JobRepository

logger = logging.getLogger(__name__)


class LocalFileStorage(FileStorage):
    """Immutable uploads and per-run assets, with cross-platform path containment."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.uploads_dir = self.base_dir / "uploads"
        self.assets_root = self.base_dir / "assets"
        self.pages_root = self.base_dir / "pages"
        for folder in (self.uploads_dir, self.assets_root, self.pages_root):
            folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _identifier(value: str) -> str:
        import re
        from app.domain.exceptions import ValidationError
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise ValidationError("Identificador de almacenamiento inválido")
        return value

    @staticmethod
    def _contained(root: Path, *parts: str) -> Path:
        from app.domain.exceptions import ValidationError
        resolved = root.joinpath(*parts).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValidationError("La ruta solicitada está fuera del almacenamiento autorizado")
        return resolved

    def save_upload(self, document_id: str, data: bytes) -> Path:
        target = self.upload_path(document_id)
        if target.exists():
            if target.read_bytes() != data:
                from app.domain.exceptions import ConflictError
                raise ConflictError("El contenido de un upload inmutable no puede reemplazarse")
            return target
        # 'xb' prevents accidental replacement of an existing revision.
        try:
            with target.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if target.read_bytes() != data:
                raise
        return target

    def save_stream(self, stream: object, max_bytes: int) -> tuple[str, str, int]:
        import hashlib
        import os
        import tempfile
        from app.domain.exceptions import ValidationError
        digest = hashlib.sha256()
        size = 0
        header = b""
        fd, temporary = tempfile.mkstemp(prefix="upload_", suffix=".tmp", dir=self.uploads_dir)
        try:
            with os.fdopen(fd, "wb") as target:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise ValidationError("El archivo excede el límite permitido")
                    header = (header + block)[:1024] if len(header) < 1024 else header
                    digest.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if size < 64 or b"%PDF-" not in header:
                raise ValidationError("El archivo no es un PDF válido")
            sha = digest.hexdigest()
            source_id = "blob_" + sha
            target_path = self.upload_path(source_id)
            # Install atomically without replacing an open/existing blob (Windows and Linux).
            try:
                os.link(temporary, target_path)
            except FileExistsError:
                existing = hashlib.sha256()
                with target_path.open("rb") as current:
                    for block in iter(lambda: current.read(1024 * 1024), b""):
                        existing.update(block)
                if target_path.stat().st_size != size or existing.hexdigest() != sha:
                    from app.domain.exceptions import ConflictError
                    raise ConflictError("Un blob existente no coincide con su hash de contenido")
            return source_id, sha, size
        finally:
            Path(temporary).unlink(missing_ok=True)

    def upload_path(self, document_id: str) -> Path:
        return self._contained(self.uploads_dir, self._identifier(document_id) + ".pdf")

    def assets_dir(self, document_id: str) -> Path:
        folder = self._contained(self.assets_root, self._identifier(document_id))
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def asset_path(self, document_id: str, file_name: str) -> Path:
        import re
        from app.domain.exceptions import ValidationError
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,180}", file_name) or ".." in file_name:
            raise ValidationError("Nombre de activo inválido")
        return self._contained(self.assets_root, self._identifier(document_id), file_name)

    def page_image_path(self, document_id: str, page: int) -> Path:
        from app.domain.exceptions import ValidationError
        if not 1 <= page <= 10000:
            raise ValidationError("Número de página inválido")
        return self._contained(self.pages_root, self._identifier(document_id), f"page_{page:03d}.png")

    def manifest_path(self, run_id: str) -> Path:
        root = self.base_dir / "manifests"
        root.mkdir(exist_ok=True)
        return self._contained(root, self._identifier(run_id) + ".json")

    def write_manifest(self, run_id: str, manifest: dict) -> Path:
        import os
        path = self.manifest_path(run_id)
        text = json.dumps(manifest, ensure_ascii=False, default=lambda v: v.value)
        with path.open("x", encoding="utf-8") as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        return path

    def delete_document(self, document_id: str) -> None:
        # Explicit administrative GC only; catalog tombstones revoke access first.
        self.upload_path(document_id).unlink(missing_ok=True)
        for base in (self.assets_root, self.pages_root):
            target = self._contained(base, self._identifier(document_id))
            if target.exists():
                shutil.rmtree(target)


# --------------------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------------------
def _job_to_dict(job: IngestionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "file_name": job.file_name,
        "status": job.status.value,
        "progress": job.progress,
        "stage": job.stage,
        "message": job.message,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _job_from_dict(data: dict[str, Any]) -> IngestionJob:
    return IngestionJob(
        job_id=data["job_id"],
        document_id=data["document_id"],
        file_name=data["file_name"],
        status=JobStatus(data["status"]),
        progress=float(data.get("progress", 0.0)),
        stage=data.get("stage", "queued"),
        message=data.get("message", ""),
        error=data.get("error"),
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
        finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None,
    )


def _doc_to_dict(doc: Document) -> dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "file_name": doc.file_name,
        "sha256": doc.sha256,
        "size_bytes": doc.size_bytes,
        "status": doc.status.value,
        "page_count": doc.page_count,
        "chunk_count": doc.chunk_count,
        "visual_count": doc.visual_count,
        "table_count": doc.table_count,
        "job_id": doc.job_id,
        "created_at": doc.created_at.isoformat(),
        "indexed_at": doc.indexed_at.isoformat() if doc.indexed_at else None,
    }


def _doc_from_dict(data: dict[str, Any]) -> Document:
    return Document(
        document_id=data["document_id"],
        file_name=data["file_name"],
        sha256=data["sha256"],
        size_bytes=int(data["size_bytes"]),
        status=JobStatus(data["status"]),
        page_count=int(data.get("page_count", 0)),
        chunk_count=int(data.get("chunk_count", 0)),
        visual_count=int(data.get("visual_count", 0)),
        table_count=int(data.get("table_count", 0)),
        job_id=data.get("job_id"),
        created_at=datetime.fromisoformat(data["created_at"]),
        indexed_at=datetime.fromisoformat(data["indexed_at"]) if data.get("indexed_at") else None,
    )


class PersistentJobRepository(JobRepository):
    """Thread-safe job repository persisted to JSON (and optionally Redis for multi-worker)."""

    def __init__(self, state_dir: Path | None = None, redis_client: Any | None = None) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, IngestionJob] = {}
        self._redis = redis_client
        self._file: Path | None = None
        if state_dir is not None:
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            self._file = Path(state_dir) / "jobs.json"
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        if self._file and self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                self._jobs = {k: _job_from_dict(v) for k, v in raw.items()}
            except Exception as exc:
                logger.warning("Could not load jobs state file: %s", exc)

    def _flush_to_disk(self) -> None:
        if self._file is not None:
            payload = {k: _job_to_dict(v) for k, v in self._jobs.items()}
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._file)

    def save(self, job: IngestionJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._flush_to_disk()
            if self._redis is not None:
                try:
                    self._redis.set(f"rag:job:{job.job_id}", json.dumps(_job_to_dict(job)))
                except Exception as exc:
                    logger.warning("Redis job save fallback to disk: %s", exc)

    def get(self, job_id: str) -> IngestionJob | None:
        with self._lock:
            if self._redis is not None:
                try:
                    raw = self._redis.get(f"rag:job:{job_id}")
                    if raw:
                        return _job_from_dict(json.loads(raw))
                except Exception:
                    pass
            self._load_from_disk()
            return self._jobs.get(job_id)


class PersistentDocumentRepository(DocumentRepository):
    """Thread-safe document registry persisted to JSON (and optionally Redis)."""

    def __init__(self, state_dir: Path | None = None, redis_client: Any | None = None) -> None:
        self._lock = threading.RLock()
        self._docs: dict[str, Document] = {}
        self._redis = redis_client
        self._file: Path | None = None
        if state_dir is not None:
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            self._file = Path(state_dir) / "documents.json"
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        if self._file and self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                self._docs = {k: _doc_from_dict(v) for k, v in raw.items()}
            except Exception as exc:
                logger.warning("Could not load documents state file: %s", exc)

    def _flush_to_disk(self) -> None:
        if self._file is not None:
            payload = {k: _doc_to_dict(v) for k, v in self._docs.items()}
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._file)

    def save(self, document: Document) -> None:
        with self._lock:
            self._docs[document.document_id] = document
            self._flush_to_disk()
            if self._redis is not None:
                try:
                    self._redis.hset("rag:documents", document.document_id, json.dumps(_doc_to_dict(document)))
                except Exception as exc:
                    logger.warning("Redis document save fallback to disk: %s", exc)

    def get(self, document_id: str) -> Document | None:
        with self._lock:
            if self._redis is not None:
                try:
                    raw = self._redis.hget("rag:documents", document_id)
                    if raw:
                        return _doc_from_dict(json.loads(raw))
                except Exception:
                    pass
            self._load_from_disk()
            return self._docs.get(document_id)

    def find_by_hash(self, sha256: str) -> Document | None:
        for doc in self.list():
            if doc.sha256 == sha256:
                return doc
        return None

    def list(self) -> list[Document]:
        with self._lock:
            if self._redis is not None:
                try:
                    raw_map = self._redis.hgetall("rag:documents")
                    if raw_map:
                        docs = [_doc_from_dict(json.loads(v)) for v in raw_map.values()]
                        return sorted(docs, key=lambda d: d.created_at, reverse=True)
                except Exception:
                    pass
            self._load_from_disk()
            return sorted(self._docs.values(), key=lambda d: d.created_at, reverse=True)

    def delete(self, document_id: str) -> None:
        with self._lock:
            self._docs.pop(document_id, None)
            self._flush_to_disk()
            if self._redis is not None:
                try:
                    self._redis.hdel("rag:documents", document_id)
                except Exception:
                    pass
