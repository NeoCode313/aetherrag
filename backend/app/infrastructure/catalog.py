"""Transactional document/job catalog with durable outbox and fenced publication.

PostgreSQL is the production authority. SQLite is explicitly opt-in for tests;
its write serialization is not a claim about distributed PostgreSQL behavior.
External effects are at-least-once: each attempt gets an isolated run namespace.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import and_, create_engine, delete, event, func, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.pool import StaticPool

from app.domain.entities import Document, IngestionJob, JobStatus
from app.domain.exceptions import CapacityError, ConflictError, DependencyUnavailableError, NotFoundError, ValidationError
from app.infrastructure import catalog_schema as s
from app.infrastructure.catalog_migrations import migrate

ACTIVE_STATES = ("queued", "running", "retry_wait")
STAT_FIELDS = ("page_count", "chunk_count", "visual_count", "table_count")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _datetime(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _secret_free(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lower = str(key).lower()
            if any(part in lower for part in ("api_key", "password", "secret", "access_token")):
                if not lower.endswith("_ref"):
                    raise ValidationError("Profiles store secret references, never credentials")
            _secret_free(child)
    elif isinstance(value, list):
        for child in value:
            _secret_free(child)


class Catalog:
    def __init__(
        self, database_url: str, *, allow_sqlite: bool = False, auto_migrate: bool = True,
        workspace_id: str = "default", pipeline_version: str = "layout-v2",
        max_attempts: int = 3, max_pending_jobs: int = 1000,
    ) -> None:
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        sqlite = database_url.startswith("sqlite")
        if sqlite and not allow_sqlite:
            raise ValidationError("SQLite is restricted to explicitly enabled tests")
        if not sqlite and not database_url.startswith("postgresql"):
            raise ValidationError("The durable catalog requires PostgreSQL")
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if sqlite:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in database_url:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **kwargs)
        self._memory_lock = threading.RLock() if sqlite and ":memory:" in database_url else None
        if sqlite:
            @event.listens_for(self.engine, "connect")
            def sqlite_connect(dbapi_connection: Any, _record: Any) -> None:
                dbapi_connection.isolation_level = None
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

            @event.listens_for(self.engine, "begin")
            def sqlite_begin(connection: Connection) -> None:
                connection.exec_driver_sql("BEGIN" if connection.get_execution_options().get("catalog_read_only") else "BEGIN IMMEDIATE")
        self.workspace_id = workspace_id
        self.pipeline_version = pipeline_version
        self.max_attempts = max(1, max_attempts)
        self.max_pending_jobs = max(1, max_pending_jobs)
        if auto_migrate:
            migrate(self.engine)
        self.document_repo = CatalogDocumentRepository(self)
        self.job_repo = CatalogJobRepository(self)

    @contextmanager
    def _tx(self, *, read_only: bool = False) -> Iterator[Connection]:
        try:
            with self._memory_lock if self._memory_lock is not None else nullcontext():
                with self.engine.connect() as connection:
                    connection = connection.execution_options(catalog_read_only=read_only)
                    with connection.begin():
                        yield connection
        except (InterfaceError, OperationalError) as exc:
            raise DependencyUnavailableError("The durable SQL catalog is unavailable") from exc

    @staticmethod
    def _sql_now(connection: Connection) -> Any:
        if connection.dialect.name == "postgresql":
            return func.extract("epoch", func.clock_timestamp())
        return (func.julianday("now") - 2440587.5) * 86400.0

    @staticmethod
    def _now(connection: Connection) -> float:
        return float(connection.scalar(select(Catalog._sql_now(connection))))

    @staticmethod
    def _control(connection: Connection, *, lock: bool = False) -> dict[str, Any]:
        statement = select(s.control).where(s.control.c.control_id == 1)
        if lock:
            statement = statement.with_for_update()
        return dict(connection.execute(statement).mappings().one())

    def close(self) -> None:
        self.engine.dispose()

    def healthy(self) -> bool:
        try:
            with self._tx(read_only=True) as connection:
                return self._control(connection)["control_id"] == 1
        except Exception:
            return False

    def maintenance(self) -> dict[str, Any]:
        with self._tx(read_only=True) as connection:
            return self._control(connection) | {"dispatch_paused": self._setting(connection, "dispatch_paused") == {"paused": True}}

    def set_maintenance(
        self, *, admissions_paused: bool | None = None, activations_paused: bool | None = None,
        dispatch_paused: bool | None = None,
    ) -> dict[str, Any]:
        with self._tx() as connection:
            self._control(connection, lock=True)
            values = {key: value for key, value in {
                "admissions_paused": admissions_paused, "activations_paused": activations_paused,
            }.items() if value is not None}
            if values:
                connection.execute(update(s.control).where(s.control.c.control_id == 1).values(**values))
            if dispatch_paused is not None:
                self._set_setting(connection, "dispatch_paused", {"paused": dispatch_paused})
            return self._control(connection) | {"dispatch_paused": self._setting(connection, "dispatch_paused") == {"paused": True}}

    @staticmethod
    def _setting(connection: Connection, key: str) -> dict[str, Any] | None:
        return connection.scalar(select(s.settings.c.value).where(s.settings.c.setting_key == key))

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._tx(read_only=True) as connection:
            return self._setting(connection, key)

    def _set_setting(self, connection: Connection, key: str, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise ValidationError("Shared settings must be JSON objects")
        _secret_free(value)
        if not key or len(key) > 128:
            raise ValidationError("Invalid setting key")
        connection.execute(delete(s.settings).where(s.settings.c.setting_key == key))
        connection.execute(insert(s.settings).values(setting_key=key, value=value, updated_at=self._now(connection)))

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        with self._tx() as connection:
            self._control(connection, lock=True)
            self._set_setting(connection, key, value)

    def pending_count(self) -> int:
        with self._tx(read_only=True) as connection:
            return int(connection.scalar(select(func.count()).select_from(s.jobs).join(s.documents).where(
                s.documents.c.workspace_id == self.workspace_id, s.jobs.c.status.in_(ACTIVE_STATES))))

    def list_generations(self) -> list[dict[str, Any]]:
        with self._tx(read_only=True) as connection:
            return [self._generation_dict(row) for row in connection.execute(
                select(s.generations).order_by(s.generations.c.created_at)).mappings()]

    def ensure_generation(self, fingerprint: str, collection: str, profile: dict[str, Any]) -> dict[str, Any]:
        if not fingerprint or len(fingerprint) > 255 or not collection or len(collection) > 255:
            raise ValidationError("Generation fingerprint and collection must be nonempty identifiers")
        _secret_free(profile)
        spec = {key: value for key, value in profile.items() if key != "profile_id"}
        canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False)
        profile_id = str(profile.get("profile_id") or "profile_" + hashlib.sha256(canonical.encode()).hexdigest()[:20])
        generation_id = "gen_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
        with self._tx() as connection:
            control = self._control(connection, lock=True)
            old_profile = connection.execute(select(s.profiles).where(s.profiles.c.profile_id == profile_id)).mappings().first()
            if old_profile and old_profile["spec"] != spec:
                raise ConflictError("A model profile is immutable; create a new profile ID")
            if not old_profile:
                connection.execute(insert(s.profiles).values(profile_id=profile_id, spec=spec, created_at=self._now(connection)))
            old = connection.execute(select(s.generations).where(s.generations.c.fingerprint == fingerprint)).mappings().first()
            if old:
                if old["collection_name"] != collection or old["profile_id"] != profile_id:
                    raise ConflictError("The generation fingerprint already names a different configuration")
                return self._generation_dict(old)
            collection_owner = connection.scalar(select(s.generations.c.generation_id).where(s.generations.c.collection_name == collection))
            if collection_owner:
                raise ConflictError("Each generation requires its own physical collection")
            first = control["active_generation_id"] is None
            connection.execute(insert(s.generations).values(
                generation_id=generation_id, fingerprint=fingerprint, collection_name=collection,
                profile_id=profile_id, status="active" if first else "building", created_at=self._now(connection),
            ))
            if first:
                connection.execute(update(s.control).where(s.control.c.control_id == 1).values(active_generation_id=generation_id))
            return self._generation_dict(connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().one())

    @staticmethod
    def _generation_dict(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["collection"] = result["collection_name"]
        return result

    def generation_by_id(self, generation_id: str) -> dict[str, Any] | None:
        with self._tx(read_only=True) as connection:
            row = connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().first()
            return self._generation_dict(row) if row else None

    def profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        with self._tx(read_only=True) as connection:
            row = connection.execute(select(s.profiles).where(s.profiles.c.profile_id == profile_id)).mappings().first()
            return {"profile_id": row["profile_id"], **row["spec"]} if row else None

    def active_generation(self) -> dict[str, Any] | None:
        with self._tx(read_only=True) as connection:
            generation_id = self._control(connection)["active_generation_id"]
            row = connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().first()
            return self._generation_dict(row) if row else None

    def _generation_manifest(self, connection: Connection, generation_id: str) -> dict[str, Any]:
        selected = select(s.activations.c.document_id, s.activations.c.run_id, s.runs.c.revision_id,
                          s.runs.c.manifest_path, s.runs.c.document_stats,
                          s.revisions.c.pipeline_version, s.documents.c.sha256).select_from(
            s.activations.join(s.runs, s.activations.c.run_id == s.runs.c.run_id).join(
            s.documents, s.activations.c.document_id == s.documents.c.document_id).join(
            s.revisions, s.runs.c.revision_id == s.revisions.c.revision_id)).where(
            s.activations.c.generation_id == generation_id,
            s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None),
        )
        rows = [dict(row) for row in connection.execute(selected).mappings()]
        active = self._control(connection)["active_generation_id"]
        current = connection.execute(select(s.activations.c.document_id, s.documents.c.sha256).select_from(
            s.activations.join(s.runs, s.activations.c.run_id == s.runs.c.run_id).join(
            s.documents, s.activations.c.document_id == s.documents.c.document_id)).where(
            s.activations.c.generation_id == active, s.documents.c.workspace_id == self.workspace_id,
            s.documents.c.deleted_at.is_(None),
        )).all()
        # Documents are immutable content identities (workspace, SHA-256). A parser
        # upgrade legitimately creates a different extraction revision for the same
        # PDF; requiring revision-ID equality would make that cutover impossible.
        target_profile = connection.scalar(select(s.profiles.c.spec).join(s.generations,
            s.generations.c.profile_id == s.profiles.c.profile_id).where(s.generations.c.generation_id == generation_id)) or {}
        expected_pipeline = target_profile.get("pipeline_version")
        by_document = {row["document_id"]: row for row in rows}
        missing = [document_id for document_id, sha256 in current if
            document_id not in by_document or by_document[document_id]["sha256"] != sha256
            or (expected_pipeline and by_document[document_id]["pipeline_version"] != expected_pipeline)]
        return {"generation_id": generation_id, "runs": rows, "run_ids": [r["run_id"] for r in rows],
                "document_count": len(rows), "missing_document_ids": sorted(missing)}

    def generation_manifest(self, generation_id: str) -> dict[str, Any]:
        with self._tx(read_only=True) as connection:
            return self._generation_manifest(connection, generation_id)

    def activate_generation(self, generation_id: str, *, expected_active_id: str | None = None) -> bool:
        with self._tx() as connection:
            control = self._control(connection, lock=True)
            current = control["active_generation_id"]
            if expected_active_id is not None and current != expected_active_id:
                raise ConflictError("The active generation changed while preparing the cutover")
            target = connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().first()
            if not target:
                raise NotFoundError("Unknown index generation")
            if current == generation_id:
                return True
            if current:
                if not control["activations_paused"]:
                    raise ConflictError("Pause document activations before a generation cutover")
                running = connection.scalar(select(func.count()).select_from(s.jobs).where(
                    s.jobs.c.generation_id == current, s.jobs.c.status == "running"))
                if running:
                    raise ConflictError("Drain running jobs in the previous generation before cutover")
                if self._generation_manifest(connection, generation_id)["missing_document_ids"]:
                    raise ConflictError("The target generation does not cover every active document revision")
                connection.execute(update(s.generations).where(s.generations.c.generation_id == current).values(status="retired"))
            connection.execute(update(s.generations).where(s.generations.c.generation_id == generation_id).values(status="active"))
            connection.execute(update(s.control).where(s.control.c.control_id == 1).values(active_generation_id=generation_id))
            # Never change a queued job's immutable embedding profile. Atomically
            # supersede it and create/reuse equivalent work in the new generation.
            if current:
                now = self._now(connection)
                pending = connection.execute(select(s.jobs).where(s.jobs.c.generation_id == current,
                    s.jobs.c.status.in_(("queued", "retry_wait"))).order_by(s.jobs.c.job_id)).mappings().all()
                for old in pending:
                    target_spec = connection.scalar(select(s.profiles.c.spec).where(s.profiles.c.profile_id == target['profile_id']))
                    pipeline = target_spec.get("pipeline_version", self.pipeline_version)
                    revision_id = old["revision_id"]
                    if pipeline != old["pipeline_version"]:
                        source = connection.scalar(select(s.revisions.c.source_id).where(s.revisions.c.revision_id == revision_id))
                        revision_id = connection.scalar(select(s.revisions.c.revision_id).where(
                            s.revisions.c.document_id == old["document_id"], s.revisions.c.source_id == source,
                            s.revisions.c.pipeline_version == pipeline).order_by(s.revisions.c.created_at.desc()).limit(1))
                        if revision_id is None:
                            revision_id = _id("rev")
                            connection.execute(insert(s.revisions).values(revision_id=revision_id,
                                document_id=old["document_id"], source_id=source, pipeline_version=pipeline, created_at=now))
                    active_key = hashlib.sha256(f"{old['document_id']}:{pipeline}:{target['profile_id']}:{generation_id}".encode()).hexdigest()
                    replacement = connection.scalar(select(s.jobs.c.job_id).where(s.jobs.c.active_key == active_key))
                    if not replacement:
                        replacement = _id("job")
                        connection.execute(insert(s.jobs).values(job_id=replacement, document_id=old["document_id"],
                            revision_id=revision_id, generation_id=generation_id, profile_id=target["profile_id"],
                            pipeline_version=pipeline, active_key=active_key, status="queued", progress=0,
                            stage="queued", message="Trabajo migrado a la generación activa", created_at=now,
                            updated_at=now, fencing_epoch=0, attempt_count=0, max_attempts=old["max_attempts"], dispatch_seq=1))
                        self._emit(connection, replacement, 1, now)
                    connection.execute(update(s.jobs).where(s.jobs.c.job_id == old["job_id"]).values(
                        status="cancelled", active_key=None, stage="superseded", finished_at=now,
                        updated_at=now, replacement_job_id=replacement, message=f"Sustituido por {replacement}"))
                    connection.execute(update(s.documents).where(s.documents.c.document_id == old["document_id"],
                        s.documents.c.job_id == old["job_id"]).values(job_id=replacement))
            return True

    def submit(
        self, file_name: str, sha256: str, size_bytes: int, source_id: str,
        generation_id: str, profile_id: str, force: bool = False,
        idempotency_key: str | None = None, administrative: bool = False,
    ) -> tuple[IngestionJob, bool]:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise ValidationError("Content SHA-256 must be a hexadecimal digest")
        if size_bytes <= 0 or not source_id or not file_name or len(file_name) > 512:
            raise ValidationError("Upload metadata is incomplete")
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 255):
            raise ValidationError("Idempotency-Key must contain 1-255 characters")
        sha256 = sha256.lower()
        with self._tx() as connection:
            control = self._control(connection, lock=True)
            if control["admissions_paused"] and not administrative:
                raise CapacityError("Catalog admissions are paused for maintenance")
            generation = connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().first()
            if not generation or generation["profile_id"] != profile_id or generation["status"] == "retired":
                raise ConflictError("The job profile and writable generation must match")
            profile = connection.scalar(select(s.profiles.c.spec).where(s.profiles.c.profile_id == profile_id))
            if profile.get("pipeline_version", self.pipeline_version) != self.pipeline_version:
                raise ConflictError("El índice requiere recuperación. Inicia sesión y pulsa Recuperar índice anterior.")
            if idempotency_key:
                previous = connection.execute(select(s.idempotency).where(
                    s.idempotency.c.workspace_id == self.workspace_id,
                    s.idempotency.c.request_key == idempotency_key)).mappings().first()
                if previous:
                    if previous["sha256"] != sha256:
                        raise ConflictError("Idempotency-Key was used with different content")
                    return self._job(connection, previous["job_id"]), True
            document = connection.execute(select(s.documents).where(s.documents.c.workspace_id == self.workspace_id,
                s.documents.c.sha256 == sha256).with_for_update()).mappings().first()
            if document and document["deleted_at"] is None:
                equivalent = and_(s.jobs.c.document_id == document["document_id"],
                    s.jobs.c.generation_id == generation_id, s.jobs.c.profile_id == profile_id,
                    s.jobs.c.pipeline_version == self.pipeline_version)
                existing = connection.execute(select(s.jobs.c.job_id).where(equivalent,
                    or_(s.jobs.c.status.in_(ACTIVE_STATES), and_(s.jobs.c.status == "completed", not force)))
                    .order_by(s.jobs.c.created_at.desc())).scalar()
                if existing:
                    if idempotency_key:
                        connection.execute(insert(s.idempotency).values(workspace_id=self.workspace_id,
                            request_key=idempotency_key, sha256=sha256, job_id=existing))
                    return self._job(connection, existing), True
            pending = connection.scalar(select(func.count()).select_from(s.jobs).join(s.documents).where(
                s.documents.c.workspace_id == self.workspace_id, s.jobs.c.status.in_(ACTIVE_STATES)))
            if pending >= self.max_pending_jobs:
                raise CapacityError("Pending ingestion capacity reached")
            now = self._now(connection)
            document_id = document["document_id"] if document else _id("doc")
            job_id, revision_id = _id("job"), _id("rev")
            if not document:
                connection.execute(insert(s.documents).values(document_id=document_id, workspace_id=self.workspace_id,
                    sha256=sha256, file_name=file_name, size_bytes=size_bytes, created_at=now, job_id=job_id))
            else:
                connection.execute(update(s.documents).where(s.documents.c.document_id == document_id).values(
                    file_name=file_name, job_id=job_id, deleted_at=None))
            reusable = None if force else connection.scalar(select(s.revisions.c.revision_id).where(
                s.revisions.c.document_id == document_id, s.revisions.c.pipeline_version == self.pipeline_version)
                .order_by(s.revisions.c.created_at.desc()).limit(1))
            if reusable:
                revision_id = reusable
            else:
                connection.execute(insert(s.revisions).values(revision_id=revision_id, document_id=document_id,
                    source_id=source_id, pipeline_version=self.pipeline_version, created_at=now))
            active_key = hashlib.sha256(f"{document_id}:{self.pipeline_version}:{profile_id}:{generation_id}".encode()).hexdigest()
            connection.execute(insert(s.jobs).values(
                job_id=job_id, document_id=document_id, revision_id=revision_id,
                generation_id=generation_id, profile_id=profile_id, pipeline_version=self.pipeline_version,
                active_key=active_key, status="queued", progress=0, stage="queued", message="Documento aceptado de forma durable",
                created_at=now, updated_at=now, fencing_epoch=0, attempt_count=0,
                max_attempts=self.max_attempts, dispatch_seq=1,
            ))
            self._emit(connection, job_id, 1, now)
            if idempotency_key:
                connection.execute(insert(s.idempotency).values(workspace_id=self.workspace_id,
                    request_key=idempotency_key, sha256=sha256, job_id=job_id))
            return self._job(connection, job_id), False

    @staticmethod
    def _emit(connection: Connection, job_id: str, seq: int, now: float) -> None:
        connection.execute(insert(s.outbox).values(event_id=_id("evt"), job_id=job_id,
            dispatch_seq=seq, created_at=now, available_at=now))

    def _row(self, connection: Connection, job_id: str, *, lock: bool = False) -> dict[str, Any] | None:
        doc_id = connection.scalar(select(s.jobs.c.document_id).where(s.jobs.c.job_id == job_id))
        if not doc_id:
            return None
        document_query = select(s.documents).where(s.documents.c.document_id == doc_id,
            s.documents.c.workspace_id == self.workspace_id)
        if lock:
            document_query = document_query.with_for_update()
        document = connection.execute(document_query).mappings().first()
        if not document:
            return None
        query = select(s.jobs).where(s.jobs.c.job_id == job_id)
        if lock:
            query = query.with_for_update()
        row = dict(connection.execute(query).mappings().one())
        row["file_name"], row["deleted_at"] = document["file_name"], document["deleted_at"]
        return row

    @staticmethod
    def _job_entity(row: dict[str, Any]) -> IngestionJob:
        status = {"queued": JobStatus.PENDING, "retry_wait": JobStatus.PENDING,
            "running": JobStatus.PROCESSING, "completed": JobStatus.COMPLETED,
            "failed": JobStatus.FAILED, "cancelled": JobStatus.FAILED}[row["status"]]
        return IngestionJob(job_id=row["job_id"], document_id=row["document_id"],
            file_name=row["file_name"], status=status, progress=row["progress"],
            stage=row["stage"], message=row["message"], error=row["error"],
            created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
            started_at=_datetime(row["started_at"]), finished_at=_datetime(row["finished_at"]))

    def _job(self, connection: Connection, job_id: str) -> IngestionJob:
        row = self._row(connection, job_id)
        if not row:
            raise NotFoundError("Unknown ingestion job")
        return self._job_entity(row)

    def _context(self, connection: Connection, row: dict[str, Any]) -> dict[str, Any]:
        revision = connection.execute(select(s.revisions).where(s.revisions.c.revision_id == row["revision_id"])).mappings().one()
        generation = connection.execute(select(s.generations).where(s.generations.c.generation_id == row["generation_id"])).mappings().one()
        return {key: row[key] for key in ("job_id", "document_id", "revision_id", "generation_id", "profile_id", "pipeline_version")} | {
            "source_id": revision["source_id"], "collection_name": generation["collection_name"],
            "collection": generation["collection_name"], "file_name": row["file_name"],
            "status": row["status"], "run_id": row["run_id"], "epoch": row["fencing_epoch"],
            "replacement_job_id": row["replacement_job_id"],
        }

    def job_context(self, job_id: str) -> dict[str, Any]:
        with self._tx(read_only=True) as connection:
            row = self._row(connection, job_id)
            if not row:
                raise NotFoundError("Unknown ingestion job")
            return self._context(connection, row)

    def claim(self, job_id: str, owner: str, lease_seconds: float = 120) -> dict[str, Any] | None:
        if not owner or len(owner) > 255 or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValidationError("A lease needs a bounded owner and positive duration")
        with self._tx() as connection:
            self._control(connection, lock=True)
            if self._setting(connection, "dispatch_paused") == {"paused": True}:
                return None
            row = self._row(connection, job_id, lock=True)
            if not row or row["deleted_at"] is not None or row["status"] != "queued":
                return None
            generation = connection.scalar(select(s.generations.c.status).where(s.generations.c.generation_id == row["generation_id"]))
            if generation == "retired":
                return None
            now = self._now(connection)
            epoch, run_id = row["fencing_epoch"] + 1, _id("run")
            connection.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(
                status="running", stage="parsing", updated_at=now, started_at=row["started_at"] or now,
                lease_owner=owner, lease_expires_at=now + lease_seconds, fencing_epoch=epoch,
                attempt_count=row["attempt_count"] + 1, run_id=run_id, error=None,
            ))
            connection.execute(insert(s.runs).values(run_id=run_id, job_id=job_id,
                document_id=row["document_id"], revision_id=row["revision_id"], generation_id=row["generation_id"],
                fencing_epoch=epoch, status="running", created_at=now))
            row = self._row(connection, job_id)
            context = self._context(connection, row)
            return {**context, "job": self._job_entity(row), "owner": owner, "context": context}

    def _owned(self, connection: Connection, job_id: str, owner: str, epoch: int) -> dict[str, Any] | None:
        row = self._row(connection, job_id, lock=True)
        if (not row or row["deleted_at"] is not None or row["status"] != "running"
            or row["lease_owner"] != owner or row["fencing_epoch"] != epoch
            or row["lease_expires_at"] is None or row["lease_expires_at"] <= self._now(connection)):
            return None
        return row

    def _ownership(self, connection: Connection, job_id: str, owner: str, epoch: int) -> Any:
        return and_(s.jobs.c.job_id == job_id, s.jobs.c.status == "running",
            s.jobs.c.lease_owner == owner, s.jobs.c.fencing_epoch == epoch,
            s.jobs.c.lease_expires_at > self._sql_now(connection))

    def heartbeat(self, job_id: str, owner: str, epoch: int, lease_seconds: float = 120) -> bool:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValidationError("Lease duration must be positive")
        with self._tx() as connection:
            row = self._owned(connection, job_id, owner, epoch)
            if not row:
                return False
            changed = connection.execute(update(s.jobs).where(self._ownership(connection, job_id, owner, epoch)).values(
                lease_expires_at=self._sql_now(connection) + lease_seconds))
            return changed.rowcount == 1

    def progress(self, job_id: str, owner: str, epoch: int, progress: float, stage: str, message: str) -> bool:
        if not math.isfinite(progress):
            raise ValidationError("Progress must be finite")
        with self._tx() as connection:
            row = self._owned(connection, job_id, owner, epoch)
            if not row:
                return False
            changed = connection.execute(update(s.jobs).where(self._ownership(connection, job_id, owner, epoch)).values(
                progress=max(row["progress"], min(99.0, max(0.0, progress))), stage=stage[:64],
                message=message, updated_at=self._now(connection)))
            return changed.rowcount == 1

    def complete(self, job_id: str, owner: str, epoch: int, document_stats: dict[str, int], manifest_path: str) -> bool:
        stats = {key: document_stats.get(key, 0) for key in STAT_FIELDS}
        if (not manifest_path or any(type(value) is not int or value < 0 for value in stats.values())
            or stats["chunk_count"] < 1):
            raise ValidationError("Publication requires a manifest and positive chunk count")
        with self._tx() as connection:
            control = self._control(connection, lock=True)
            row = self._owned(connection, job_id, owner, epoch)
            if not row or control["activations_paused"]:
                return False
            generation_status = connection.scalar(select(s.generations.c.status).where(s.generations.c.generation_id == row["generation_id"]))
            if generation_status not in ("active", "building"):
                return False
            now = self._now(connection)
            changed = connection.execute(update(s.jobs).where(self._ownership(connection, job_id, owner, epoch)).values(
                status="completed", progress=100, stage="completed", message="Indexación publicada", error=None,
                active_key=None, lease_owner=None, lease_expires_at=None, updated_at=now, finished_at=now))
            if changed.rowcount != 1:
                return False
            connection.execute(update(s.runs).where(s.runs.c.run_id == row["run_id"]).values(
                status="completed", manifest_path=manifest_path, document_stats=stats, finished_at=now))
            connection.execute(delete(s.activations).where(s.activations.c.document_id == row["document_id"],
                s.activations.c.generation_id == row["generation_id"]))
            connection.execute(insert(s.activations).values(document_id=row["document_id"],
                generation_id=row["generation_id"], run_id=row["run_id"], activated_at=now))
            return True

    def fail(self, job_id: str, owner: str, epoch: int, error: str, retryable: bool = False) -> bool:
        with self._tx() as connection:
            row = self._owned(connection, job_id, owner, epoch)
            if not row:
                return False
            now = self._now(connection)
            retry = retryable and row["attempt_count"] < row["max_attempts"]
            changed = connection.execute(update(s.jobs).where(self._ownership(connection, job_id, owner, epoch)).values(
                status="retry_wait" if retry else "failed", stage="retry_wait" if retry else "failed",
                message="Reintento pendiente" if retry else "La ingesta falló", error=error[:4000],
                next_attempt_at=now + min(300, 2 ** row["attempt_count"]) if retry else None,
                active_key=row["active_key"] if retry else None, lease_owner=None, lease_expires_at=None,
                updated_at=now, finished_at=None if retry else now))
            if changed.rowcount != 1:
                return False
            connection.execute(update(s.runs).where(s.runs.c.run_id == row["run_id"]).values(status="failed", finished_at=now))
            return True

    def active_run_ids(self, document_ids: list[str] | None = None, generation_id: str | None = None) -> list[str]:
        with self._tx(read_only=True) as connection:
            generation_id = generation_id or self._control(connection)["active_generation_id"]
            query = select(s.activations.c.run_id).select_from(s.activations.join(
                s.documents, s.activations.c.document_id == s.documents.c.document_id)).where(
                s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None),
                s.activations.c.generation_id == generation_id)
            if document_ids is not None:
                query = query.where(s.documents.c.document_id.in_(document_ids))
            return list(connection.scalars(query))

    def published_run_document(self, run_id: str) -> str | None:
        with self._tx(read_only=True) as connection:
            return connection.scalar(select(s.runs.c.document_id).join(s.documents).where(
                s.runs.c.run_id == run_id, s.runs.c.status == "completed",
                s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None)))

    def query_snapshot(self, document_ids: list[str] | None = None) -> dict[str, Any]:
        """Capture collection, profile and published runs with one SQL statement.

        The generation join belongs to the statement, so a concurrent cutover
        cannot mix the previous profile with the next generation's run filter.
        """
        with self._tx(read_only=True) as connection:
            eligible = select(s.activations.c.run_id, s.activations.c.generation_id).join(s.documents).where(
                s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None))
            if document_ids is not None:
                eligible = eligible.where(s.documents.c.document_id.in_(document_ids))
            eligible = eligible.subquery()
            llm = select(s.settings.c.value).where(s.settings.c.setting_key == "llm_profile").scalar_subquery()
            rows = connection.execute(select(s.generations, eligible.c.run_id, llm.label("llm_profile")).select_from(
                s.control.join(s.generations, s.control.c.active_generation_id == s.generations.c.generation_id)
                .outerjoin(eligible, eligible.c.generation_id == s.generations.c.generation_id))
                .where(s.control.c.control_id == 1)).mappings().all()
            if not rows:
                raise ConflictError("No active index generation")
            return {"generation": self._generation_dict({k: rows[0][k] for k in s.generations.c.keys()}),
                    "run_ids": [row["run_id"] for row in rows if row["run_id"] is not None],
                    "llm_profile": rows[0]["llm_profile"] or {}}

    def garbage_runs(self, retention_seconds: float = 86400) -> list[dict[str, Any]]:
        if not math.isfinite(retention_seconds) or retention_seconds < 0:
            raise ValidationError("Retention must be a nonnegative duration")
        with self._tx(read_only=True) as connection:
            cutoff = self._now(connection) - retention_seconds
            rows = connection.execute(select(s.runs.c.run_id, s.runs.c.status, s.runs.c.manifest_path,
                s.runs.c.document_id, s.runs.c.generation_id).select_from(s.runs.join(s.documents,
                    s.runs.c.document_id == s.documents.c.document_id)).where(
                s.documents.c.workspace_id == self.workspace_id,
                s.runs.c.status.in_(("failed", "abandoned", "cancelled")),
                s.runs.c.finished_at.is_not(None), s.runs.c.finished_at <= cutoff,
                ~select(s.activations.c.run_id).where(s.activations.c.run_id == s.runs.c.run_id).exists()))
            return [dict(row) for row in rows.mappings()]

    def ready_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx(read_only=True) as connection:
            if self._setting(connection, "dispatch_paused") == {"paused": True}:
                return []
            now = self._now(connection)
            rows = connection.execute(select(s.outbox).select_from(s.outbox.join(
                s.jobs, s.outbox.c.job_id == s.jobs.c.job_id).join(s.documents,
                s.jobs.c.document_id == s.documents.c.document_id)).where(
                s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None),
                s.jobs.c.status == "queued", s.outbox.c.dispatched_at.is_(None),
                s.outbox.c.available_at <= now).order_by(s.outbox.c.created_at).limit(max(1, min(limit, 1000)))).mappings()
            return [{**row, "id": row["event_id"]} for row in rows]

    def mark_dispatched(self, event_id: str) -> bool:
        with self._tx() as connection:
            row = connection.execute(select(s.outbox).where(s.outbox.c.event_id == event_id)).mappings().first()
            if not row:
                return False
            now = self._now(connection)
            connection.execute(update(s.outbox).where(s.outbox.c.event_id == event_id,
                s.outbox.c.dispatched_at.is_(None)).values(dispatched_at=now))
            connection.execute(update(s.jobs).where(s.jobs.c.job_id == row["job_id"],
                s.jobs.c.dispatch_seq == row["dispatch_seq"], s.jobs.c.status == "queued").values(last_dispatched_at=now))
            return True

    def reconcile(self, *, queued_timeout_seconds: float = 60, limit: int = 100) -> dict[str, int]:
        result = {"requeued": 0, "failed": 0, "examined": 0}
        with self._tx() as connection:
            now = self._now(connection)
            candidates = list(connection.scalars(select(s.jobs.c.job_id).join(s.documents).where(
                s.documents.c.workspace_id == self.workspace_id, s.documents.c.deleted_at.is_(None),
                or_(and_(s.jobs.c.status == "running", s.jobs.c.lease_expires_at <= now),
                    and_(s.jobs.c.status == "retry_wait", s.jobs.c.next_attempt_at <= now),
                    and_(s.jobs.c.status == "queued", s.jobs.c.last_dispatched_at <= now - queued_timeout_seconds)))
                .order_by(s.jobs.c.job_id).limit(max(1, min(limit, 1000)))))
        for job_id in candidates:
            with self._tx() as connection:
                row = self._row(connection, job_id, lock=True)
                now = self._now(connection)
                if not row or row["deleted_at"] is not None or row["status"] not in ACTIVE_STATES:
                    continue
                expired = row["status"] == "running" and row["lease_expires_at"] <= now
                retry_due = row["status"] == "retry_wait" and row["next_attempt_at"] <= now
                lost = row["status"] == "queued" and row["last_dispatched_at"] is not None and row["last_dispatched_at"] <= now - queued_timeout_seconds
                if not (expired or retry_due or lost):
                    continue
                result["examined"] += 1
                if expired:
                    connection.execute(update(s.runs).where(s.runs.c.run_id == row["run_id"],
                        s.runs.c.status == "running").values(status="abandoned", finished_at=now))
                if row["attempt_count"] >= row["max_attempts"]:
                    connection.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(
                        status="failed", stage="failed", active_key=None, lease_owner=None,
                        lease_expires_at=None, finished_at=now, updated_at=now,
                        error="Worker lease expired; maximum attempts reached", message="La ingesta agotó sus intentos"))
                    result["failed"] += 1
                    continue
                pending_event = connection.scalar(select(s.outbox.c.event_id).where(
                    s.outbox.c.job_id == job_id, s.outbox.c.dispatched_at.is_(None)).limit(1))
                seq = row["dispatch_seq"] + 1
                connection.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(
                    status="queued", stage="queued", updated_at=now, lease_owner=None, lease_expires_at=None,
                    next_attempt_at=None, last_dispatched_at=None, dispatch_seq=seq if not pending_event else row["dispatch_seq"],
                    message="Trabajo recuperado y pendiente de despacho"))
                if not pending_event:
                    self._emit(connection, job_id, seq, now)
                result["requeued"] += 1
        return result

    def _document_entity(self, connection: Connection, row: Any) -> Document:
        active_generation_id = self._control(connection)["active_generation_id"]
        published = connection.execute(select(s.runs.c.document_stats, s.activations.c.activated_at).select_from(
            s.runs.join(s.activations, s.runs.c.run_id == s.activations.c.run_id)).where(
            s.activations.c.document_id == row["document_id"], s.activations.c.generation_id == active_generation_id)).mappings().first()
        latest = self._row(connection, row["job_id"]) if row["job_id"] else None
        stats = published["document_stats"] if published else {}
        status = JobStatus.COMPLETED if published else self._job_entity(latest).status if latest else JobStatus.PENDING
        if not published and status == JobStatus.COMPLETED:
            # A completed build in a shadow generation is not yet serving this document.
            status = JobStatus.PENDING
        return Document(document_id=row["document_id"], file_name=row["file_name"], sha256=row["sha256"],
            size_bytes=row["size_bytes"], status=status, job_id=row["job_id"], created_at=_datetime(row["created_at"]),
            indexed_at=_datetime(published["activated_at"]) if published else None,
            **{key: stats.get(key, 0) for key in STAT_FIELDS})

    def export_snapshot(self) -> dict[str, Any]:
        """Metadata export, not a coordinated filesystem/Qdrant backup.

        Maintenance is required so multiple table reads cannot mix catalog versions.
        Jobs must be drained before export; no credential material is persisted.
        """
        with self._tx(read_only=True) as connection:
            if connection.dialect.name == "postgresql":
                # Outbox publication/reconciliation can still update metadata.
                # A stable SQL snapshot keeps this multi-table export coherent.
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            controls = self._control(connection, lock=True)
            if not controls["admissions_paused"] or not controls["activations_paused"]:
                raise ConflictError("Pause admissions and activations before exporting a catalog snapshot")
            if connection.scalar(select(func.count()).select_from(s.jobs).where(s.jobs.c.status == "running")):
                raise ConflictError("Drain running jobs before exporting a catalog snapshot")
            return {"format": "aetherrag-catalog-v2", "created_at": self._now(connection),
                "workspace_id": self.workspace_id, "tables": {
                    table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                    for table in s.metadata.sorted_tables}}

    def validate(self) -> dict[str, Any]:
        """Check catalog references and publication states without repairing data."""
        with self._tx(read_only=True) as connection:
            issues: list[str] = []
            controls = self._control(connection)
            rows = connection.execute(select(s.activations.c.run_id, s.runs.c.status,
                s.runs.c.manifest_path, s.documents.c.deleted_at).select_from(s.activations.join(
                    s.runs, s.activations.c.run_id == s.runs.c.run_id).join(
                    s.documents, s.activations.c.document_id == s.documents.c.document_id))).mappings()
            for row in rows:
                if row["status"] != "completed" or not row["manifest_path"] or row["deleted_at"] is not None:
                    issues.append(f"Invalid activation for {row['run_id']}")
            for row in connection.execute(select(s.profiles)).mappings():
                try:
                    _secret_free(row["spec"])
                except ValidationError:
                    issues.append(f"Profile contains credential material: {row['profile_id']}")
            generation_id = controls["active_generation_id"]
            if generation_id:
                state = connection.scalar(select(s.generations.c.status).where(s.generations.c.generation_id == generation_id))
                if state != "active":
                    issues.append("The serving generation pointer does not reference an active generation")
            now = self._now(connection)
            expired = int(connection.scalar(select(func.count()).select_from(s.jobs).where(
                s.jobs.c.status == "running", s.jobs.c.lease_expires_at <= now)))
            return {"ok": not issues, "issues": issues, "expired_leases": expired,
                "active_generation_id": generation_id, "checks": "SQL metadata only; verify assets and Qdrant separately"}

    def import_legacy(
        self, document_rows: list[dict[str, Any]], job_rows: list[dict[str, Any]], *,
        generation_id: str, profile_id: str,
    ) -> dict[str, int]:
        """Preserve legacy IDs/sources but enqueue rebuilds, never trust unknown vectors.

        The caller must validate source files and their hashes before this transaction.
        Historical status is retained as import metadata; current status is queued.
        Repeating an import is idempotent on existing document ID/content identity.
        """
        imported = skipped = 0
        old_jobs = {row["job_id"]: row for row in job_rows}
        with self._tx() as connection:
            controls = self._control(connection, lock=True)
            if (not controls["admissions_paused"] or not controls["activations_paused"]
                or self._setting(connection, "dispatch_paused") != {"paused": True}):
                raise ConflictError("Legacy import requires admissions, dispatch and activations paused")
            if connection.scalar(select(func.count()).select_from(s.jobs).where(s.jobs.c.status == "running")):
                raise ConflictError("Drain running jobs before legacy import")
            generation = connection.execute(select(s.generations).where(s.generations.c.generation_id == generation_id)).mappings().first()
            if not generation or generation["profile_id"] != profile_id or generation["status"] == "retired":
                raise ConflictError("Import target must be a writable generation with its matching profile")
            for source in document_rows:
                document_id, sha256 = source["document_id"], source["sha256"].lower()
                if (not document_id.startswith("doc_") or len(document_id) > 64
                    or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in document_id)
                    or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
                    raise ValidationError("Invalid legacy document identity/hash")
                existing = connection.execute(select(s.documents).where(or_(
                    s.documents.c.document_id == document_id,
                    and_(s.documents.c.workspace_id == self.workspace_id, s.documents.c.sha256 == sha256)))).mappings().first()
                if existing:
                    if existing["document_id"] != document_id or existing["sha256"] != sha256:
                        raise ConflictError("Legacy identity conflicts with an existing document; resolve explicitly")
                    skipped += 1
                    continue
                job_id = source.get("job_id") or _id("job")
                if connection.scalar(select(s.jobs.c.job_id).where(s.jobs.c.job_id == job_id)):
                    raise ConflictError("Legacy job ID already belongs to another document")
                original = old_jobs.get(job_id, {})
                now = self._now(connection)
                created_at = datetime.fromisoformat(source["created_at"]).timestamp() if source.get("created_at") else now
                revision_id = _id("rev")
                connection.execute(insert(s.documents).values(document_id=document_id,
                    workspace_id=self.workspace_id, sha256=sha256, file_name=source["file_name"],
                    size_bytes=int(source["size_bytes"]), job_id=job_id, created_at=created_at))
                connection.execute(insert(s.revisions).values(revision_id=revision_id,
                    document_id=document_id, source_id=document_id, pipeline_version=self.pipeline_version, created_at=now))
                active_key = hashlib.sha256(f"{document_id}:{self.pipeline_version}:{profile_id}:{generation_id}".encode()).hexdigest()
                connection.execute(insert(s.jobs).values(job_id=job_id, document_id=document_id,
                    revision_id=revision_id, generation_id=generation_id, profile_id=profile_id,
                    pipeline_version=self.pipeline_version, active_key=active_key, status="queued", progress=0,
                    stage="legacy_rebuild", message="Importado; índice legacy no verificado, reconstrucción pendiente",
                    created_at=created_at, updated_at=now, fencing_epoch=0, attempt_count=0,
                    max_attempts=self.max_attempts, dispatch_seq=1))
                self._emit(connection, job_id, 1, now)
                self._set_setting(connection, f"legacy_import:{job_id}", {
                    "original_document_status": source.get("status"),
                    "original_job_status": original.get("status"),
                    "original_indexed_at": source.get("indexed_at"),
                    "imported_at": now, "vectors_verified": False,
                })
                imported += 1
        return {"imported": imported, "skipped": skipped}


class CatalogJobRepository:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def get(self, job_id: str) -> IngestionJob | None:
        with self.catalog._tx(read_only=True) as connection:
            row = self.catalog._row(connection, job_id)
            return self.catalog._job_entity(row) if row else None

    def save(self, job: IngestionJob) -> None:
        raise ConflictError("Use catalog claim/progress/complete/fail with a valid lease")


class CatalogDocumentRepository:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def get(self, document_id: str) -> Document | None:
        with self.catalog._tx(read_only=True) as connection:
            row = connection.execute(select(s.documents).where(s.documents.c.document_id == document_id,
                s.documents.c.workspace_id == self.catalog.workspace_id, s.documents.c.deleted_at.is_(None))).mappings().first()
            return self.catalog._document_entity(connection, row) if row else None

    def find_by_hash(self, sha256: str) -> Document | None:
        with self.catalog._tx(read_only=True) as connection:
            row = connection.execute(select(s.documents).where(s.documents.c.sha256 == sha256,
                s.documents.c.workspace_id == self.catalog.workspace_id, s.documents.c.deleted_at.is_(None))).mappings().first()
            return self.catalog._document_entity(connection, row) if row else None

    def list(self) -> list[Document]:
        with self.catalog._tx(read_only=True) as connection:
            active = select(s.control.c.active_generation_id).where(s.control.c.control_id == 1).scalar_subquery()
            publications = select(s.activations.c.document_id, s.activations.c.activated_at,
                s.runs.c.document_stats).join(s.runs).where(s.activations.c.generation_id == active).subquery()
            rows = connection.execute(select(s.documents, publications.c.activated_at, publications.c.document_stats,
                s.jobs.c.status.label("job_status")).select_from(s.documents.outerjoin(publications,
                    publications.c.document_id == s.documents.c.document_id).outerjoin(s.jobs,
                    s.jobs.c.job_id == s.documents.c.job_id)).where(
                s.documents.c.workspace_id == self.catalog.workspace_id, s.documents.c.deleted_at.is_(None))
                .order_by(s.documents.c.created_at.desc())).mappings().all()
            result = []
            for row in rows:
                published = row["activated_at"] is not None
                status = JobStatus.COMPLETED if published else {"queued": JobStatus.PENDING,
                    "retry_wait": JobStatus.PENDING, "running": JobStatus.PROCESSING,
                    "completed": JobStatus.COMPLETED, "failed": JobStatus.FAILED,
                    "cancelled": JobStatus.FAILED, None: JobStatus.PENDING}[row["job_status"]]
                if not published and status == JobStatus.COMPLETED:
                    status = JobStatus.PENDING
                stats = row["document_stats"] or {}
                result.append(Document(document_id=row["document_id"], file_name=row["file_name"], sha256=row["sha256"],
                    size_bytes=row["size_bytes"], status=status, job_id=row["job_id"], created_at=_datetime(row["created_at"]),
                    indexed_at=_datetime(row["activated_at"]), **{key: stats.get(key, 0) for key in STAT_FIELDS}))
            return result

    def save(self, document: Document) -> None:
        raise ConflictError("Document identity and status are changed only by transactional catalog operations")

    def delete(self, document_id: str) -> None:
        with self.catalog._tx() as connection:
            self.catalog._control(connection, lock=True)
            row = connection.execute(select(s.documents).where(s.documents.c.document_id == document_id,
                s.documents.c.workspace_id == self.catalog.workspace_id).with_for_update()).mappings().first()
            if not row:
                raise NotFoundError("Unknown document")
            now = self.catalog._now(connection)
            connection.execute(update(s.documents).where(s.documents.c.document_id == document_id).values(deleted_at=now))
            connection.execute(delete(s.activations).where(s.activations.c.document_id == document_id))
            connection.execute(update(s.runs).where(s.runs.c.document_id == document_id,
                s.runs.c.status == "running").values(status="cancelled", finished_at=now))
            connection.execute(update(s.jobs).where(s.jobs.c.document_id == document_id,
                s.jobs.c.status.in_(ACTIVE_STATES)).values(status="cancelled", stage="cancelled", active_key=None,
                lease_owner=None, lease_expires_at=None, finished_at=now, updated_at=now,
                message="Documento eliminado"))
