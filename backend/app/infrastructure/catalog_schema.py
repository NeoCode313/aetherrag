"""Version-one relational catalog schema; Qdrant and Redis are derived services."""

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, Float, ForeignKey, Index, Integer,
    MetaData, String, Table, Text, UniqueConstraint,
)

metadata = MetaData()

profiles = Table(
    "model_profiles", metadata,
    Column("profile_id", String(128), primary_key=True),
    Column("spec", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
generations = Table(
    "index_generations", metadata,
    Column("generation_id", String(64), primary_key=True),
    Column("fingerprint", String(255), nullable=False, unique=True),
    Column("collection_name", String(255), nullable=False, unique=True),
    Column("profile_id", ForeignKey(profiles.c.profile_id), nullable=False),
    Column("status", String(24), nullable=False),
    Column("created_at", Float, nullable=False),
)
control = Table(
    "catalog_control", metadata,
    Column("control_id", Integer, primary_key=True),
    Column("active_generation_id", ForeignKey(generations.c.generation_id)),
    Column("admissions_paused", Boolean, nullable=False, default=False),
    Column("activations_paused", Boolean, nullable=False, default=False),
)
documents = Table(
    "documents", metadata,
    Column("document_id", String(64), primary_key=True),
    Column("workspace_id", String(128), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("file_name", String(512), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("job_id", String(64)),
    Column("created_at", Float, nullable=False),
    Column("deleted_at", Float),
    UniqueConstraint("workspace_id", "sha256", name="uq_document_content"),
)
revisions = Table(
    "document_revisions", metadata,
    Column("revision_id", String(64), primary_key=True),
    Column("document_id", ForeignKey(documents.c.document_id), nullable=False),
    Column("source_id", String(128), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
    Column("created_at", Float, nullable=False),
)
jobs = Table(
    "ingestion_jobs", metadata,
    Column("job_id", String(64), primary_key=True),
    Column("document_id", ForeignKey(documents.c.document_id), nullable=False),
    Column("revision_id", ForeignKey(revisions.c.revision_id), nullable=False),
    Column("generation_id", ForeignKey(generations.c.generation_id), nullable=False),
    Column("profile_id", ForeignKey(profiles.c.profile_id), nullable=False),
    Column("pipeline_version", String(128), nullable=False),
    # NULL for terminal jobs; SQL uniqueness coordinates concurrent submissions.
    Column("active_key", String(64), unique=True),
    Column("status", String(24), nullable=False),
    Column("progress", Float, nullable=False),
    Column("stage", String(64), nullable=False),
    Column("message", Text, nullable=False),
    Column("error", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("lease_owner", String(255)),
    Column("lease_expires_at", Float),
    Column("fencing_epoch", Integer, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("run_id", String(64)),
    Column("next_attempt_at", Float),
    Column("last_dispatched_at", Float),
    Column("dispatch_seq", Integer, nullable=False),
    Column("replacement_job_id", String(64)),
)
Index("ix_jobs_recovery", jobs.c.status, jobs.c.lease_expires_at, jobs.c.next_attempt_at)
runs = Table(
    "index_runs", metadata,
    Column("run_id", String(64), primary_key=True),
    Column("job_id", ForeignKey(jobs.c.job_id), nullable=False),
    Column("document_id", ForeignKey(documents.c.document_id), nullable=False),
    Column("revision_id", ForeignKey(revisions.c.revision_id), nullable=False),
    Column("generation_id", ForeignKey(generations.c.generation_id), nullable=False),
    Column("fencing_epoch", Integer, nullable=False),
    Column("status", String(24), nullable=False),
    Column("manifest_path", Text),
    Column("document_stats", JSON),
    Column("created_at", Float, nullable=False),
    Column("finished_at", Float),
)
activations = Table(
    "document_index_activations", metadata,
    Column("document_id", ForeignKey(documents.c.document_id), primary_key=True),
    Column("generation_id", ForeignKey(generations.c.generation_id), primary_key=True),
    Column("run_id", ForeignKey(runs.c.run_id), nullable=False),
    Column("activated_at", Float, nullable=False),
)
outbox = Table(
    "outbox_events", metadata,
    Column("event_id", String(64), primary_key=True),
    Column("job_id", ForeignKey(jobs.c.job_id), nullable=False),
    Column("dispatch_seq", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("available_at", Float, nullable=False),
    Column("dispatched_at", Float),
    UniqueConstraint("job_id", "dispatch_seq", name="uq_outbox_dispatch"),
)
Index("ix_outbox_ready", outbox.c.dispatched_at, outbox.c.available_at)
idempotency = Table(
    "upload_idempotency", metadata,
    Column("workspace_id", String(128), primary_key=True),
    Column("request_key", String(255), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("job_id", ForeignKey(jobs.c.job_id), nullable=False),
)
settings = Table(
    "catalog_settings", metadata,
    Column("setting_key", String(128), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)
