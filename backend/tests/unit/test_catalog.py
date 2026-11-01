"""Catalog invariants, using explicit SQLite test adapters (no external services)."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, update

from app.domain.entities import JobStatus
from app.domain.exceptions import CapacityError, ConflictError, ValidationError
from app.infrastructure.catalog import Catalog
from app.infrastructure import catalog_schema as s


@pytest.fixture
def catalog(tmp_path):
    instance = Catalog(f"sqlite:///{(tmp_path / 'catalog.sqlite').as_posix()}", allow_sqlite=True)
    instance.ensure_generation("local-v1-8-normalized", "test_index_v1", {
        "provider": "local", "model": "hash-v1", "dimension": 8, "normalization": "l2",
    })
    yield instance
    instance.close()


def submit(catalog, digest="a", **kwargs):
    generation = kwargs.pop("generation", None) or catalog.active_generation()
    return catalog.submit("manual.pdf", digest * 64, 100, "src_123456789abc",
        generation["generation_id"], generation["profile_id"], **kwargs)


def complete(catalog, claim):
    return catalog.complete(claim["job_id"], claim["owner"], claim["epoch"], {
        "page_count": 2, "chunk_count": 5, "visual_count": 1, "table_count": 1,
    }, f"runs/{claim['run_id']}/manifest.json")


def expire(catalog, job_id):
    with catalog.engine.begin() as connection:
        connection.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(lease_expires_at=0))


def test_concurrent_identical_uploads_create_one_job_and_outbox(catalog):
    with ThreadPoolExecutor(max_workers=12) as executor:
        accepted = list(executor.map(lambda _: submit(catalog), range(100)))
    assert len({job.job_id for job, _ in accepted}) == 1
    assert sum(not duplicate for _, duplicate in accepted) == 1
    assert len(catalog.document_repo.list()) == 1
    assert len(catalog.ready_outbox()) == 1


def test_duplicate_delivery_claims_only_one_owner(catalog):
    job, _ = submit(catalog)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda i: catalog.claim(job.job_id, f"worker-{i}"), range(16)))
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim)
    assert catalog.active_run_ids() == []
    assert catalog.published_run_document(winner["run_id"]) is None
    assert complete(catalog, winner)
    assert catalog.claim(job.job_id, "late-duplicate") is None


@pytest.mark.parametrize("measurements", [
    {"parse_ms": float("nan")}, {"total_ms": -1}, {"parse_ms": True},
    {"api_key": 123}, {"parse_ms": "private text"},
])
def test_ingestion_telemetry_only_accepts_finite_numeric_known_fields(catalog, measurements):
    job, _ = submit(catalog)
    claim = catalog.claim(job.job_id, "worker")
    with pytest.raises(ValidationError, match="timing"):
        catalog.complete(job.job_id, "worker", claim["epoch"], {
            "chunk_count": 1, "page_count": 1, "timings_ms": measurements,
        }, "manifest.json")
    assert catalog.active_run_ids() == []


def test_stale_owner_cannot_change_timings_or_expose_an_unpublished_source(catalog):
    job, _ = submit(catalog)
    claim = catalog.claim(job.job_id, "old")
    assert catalog.published_run_source(claim["run_id"]) is None
    expire(catalog, job.job_id)
    assert not catalog.fail(job.job_id, "old", claim["epoch"], "late", timings_ms={"parse_ms": 9})
    assert catalog.job_timings(job.job_id) == {}


def test_expired_owner_cannot_heartbeat_update_or_publish(catalog):
    job, _ = submit(catalog)
    old = catalog.claim(job.job_id, "old")
    expire(catalog, job.job_id)
    assert not catalog.heartbeat(job.job_id, "old", old["epoch"])
    assert not catalog.progress(job.job_id, "old", old["epoch"], 80, "indexing", "late")
    assert not complete(catalog, old)
    assert catalog.reconcile()["requeued"] == 1
    new = catalog.claim(job.job_id, "new")
    assert new["epoch"] > old["epoch"]
    assert new["run_id"] != old["run_id"]
    assert not complete(catalog, old)
    assert complete(catalog, new)
    assert catalog.active_run_ids() == [new["run_id"]]


def test_failed_reingestion_keeps_previous_revision_and_historical_assets(catalog):
    job, _ = submit(catalog)
    previous = catalog.claim(job.job_id, "first")
    assert complete(catalog, previous)
    other, duplicate = submit(catalog, force=True)
    assert not duplicate and other.document_id == job.document_id
    current = catalog.claim(other.job_id, "second")
    assert current["revision_id"] != previous["revision_id"]
    assert catalog.fail(other.job_id, "second", current["epoch"], "bad extraction")
    assert catalog.active_run_ids() == [previous["run_id"]]
    assert catalog.document_repo.get(job.document_id).status == JobStatus.COMPLETED
    assert catalog.published_run_document(previous["run_id"]) == job.document_id


def test_outbox_survives_publish_failure_and_recovers_lost_broker_message(catalog):
    job, _ = submit(catalog)
    event = catalog.ready_outbox()[0]
    # A publisher failure does not call mark_dispatched: the event remains ready.
    assert catalog.ready_outbox()[0]["event_id"] == event["event_id"]
    catalog.mark_dispatched(event["event_id"])
    assert catalog.ready_outbox() == []
    # Simulate Redis losing an acknowledged publish before a worker claims it.
    with catalog.engine.begin() as connection:
        connection.execute(update(s.jobs).where(s.jobs.c.job_id == job.job_id).values(last_dispatched_at=0))
    assert catalog.reconcile()["requeued"] == 1
    recovered = catalog.ready_outbox()
    assert len(recovered) == 1
    assert recovered[0]["job_id"] == job.job_id
    assert recovered[0]["event_id"] != event["event_id"]


def test_retry_wait_is_deduplicated_and_calendar_is_durable(catalog):
    job, _ = submit(catalog)
    claim = catalog.claim(job.job_id, "one")
    assert catalog.fail(job.job_id, "one", claim["epoch"], "timeout", retryable=True)
    repeated, deduplicated = submit(catalog)
    assert deduplicated and repeated.job_id == job.job_id
    assert catalog.claim(job.job_id, "early") is None
    with catalog.engine.begin() as connection:
        connection.execute(update(s.jobs).where(s.jobs.c.job_id == job.job_id).values(next_attempt_at=0))
    catalog.reconcile()
    assert catalog.claim(job.job_id, "after-retry")


def test_repeated_worker_loss_is_bounded(catalog):
    job, _ = submit(catalog)
    for i in range(catalog.max_attempts):
        assert catalog.claim(job.job_id, f"worker-{i}")
        expire(catalog, job.job_id)
        catalog.reconcile()
    assert catalog.job_repo.get(job.job_id).status == JobStatus.FAILED
    assert catalog.claim(job.job_id, "forever") is None
    assert catalog.active_run_ids() == []


def test_idempotency_key_conflicts_with_other_content(catalog):
    first, _ = submit(catalog, idempotency_key="request-1")
    repeated, deduplicated = submit(catalog, idempotency_key="request-1")
    assert deduplicated and repeated.job_id == first.job_id
    with pytest.raises(ConflictError):
        submit(catalog, digest="b", idempotency_key="request-1")


def test_delete_fences_worker_and_blocks_historical_assets(catalog):
    job, _ = submit(catalog)
    first = catalog.claim(job.job_id, "one")
    assert complete(catalog, first)
    other, _ = submit(catalog, force=True)
    running = catalog.claim(other.job_id, "two")
    catalog.document_repo.delete(job.document_id)
    assert not complete(catalog, running)
    assert catalog.active_run_ids() == []
    assert catalog.published_run_document(first["run_id"]) is None
    assert catalog.document_repo.get(job.document_id) is None


def test_generation_cutover_requires_coverage_and_preserves_old_space(catalog):
    old_generation = catalog.active_generation()
    job, _ = submit(catalog)
    old = catalog.claim(job.job_id, "old")
    assert complete(catalog, old)
    target = catalog.ensure_generation("new-model-same-dimension", "test_index_v2", {
        "provider": "local", "model": "different-v2", "dimension": 8,
    })
    with pytest.raises(ConflictError):
        catalog.activate_generation(target["generation_id"])
    catalog.set_maintenance(activations_paused=True)
    with pytest.raises(ConflictError):
        catalog.activate_generation(target["generation_id"])
    catalog.set_maintenance(activations_paused=False)
    rebuilt, _ = submit(catalog, generation=target)
    new = catalog.claim(rebuilt.job_id, "new")
    assert new["revision_id"] == old["revision_id"]
    assert complete(catalog, new)
    catalog.set_maintenance(activations_paused=True)
    assert catalog.activate_generation(target["generation_id"], expected_active_id=old_generation["generation_id"])
    assert catalog.active_run_ids() == [new["run_id"]]
    assert catalog.active_run_ids(generation_id=old_generation["generation_id"]) == [old["run_id"]]
    assert catalog.published_run_document(old["run_id"]) == job.document_id


def test_cutover_replaces_pending_jobs_without_changing_original_profiles(catalog):
    old = catalog.active_generation()
    job, _ = submit(catalog)
    target = catalog.ensure_generation("new-model", "test_index_v2", {
        "provider": "local", "model": "different", "dimension": 8,
    })
    catalog.set_maintenance(dispatch_paused=True, activations_paused=True)
    assert catalog.claim(job.job_id, "stopped") is None
    assert catalog.activate_generation(target["generation_id"])
    old_context = catalog.job_context(job.job_id)
    assert old_context["profile_id"] == old["profile_id"]
    replacement = old_context["replacement_job_id"]
    assert replacement and catalog.job_context(replacement)["profile_id"] == target["profile_id"]
    catalog.set_maintenance(dispatch_paused=False, activations_paused=False)
    assert catalog.claim(replacement, "resumed")


def test_profiles_and_shared_settings_are_immutable_or_secret_free(catalog):
    profile_id = catalog.active_generation()["profile_id"]
    with pytest.raises(ConflictError):
        catalog.ensure_generation("third", "index3", {"profile_id": profile_id, "model": "changed"})
    with pytest.raises(ValidationError):
        catalog.set_setting("llm_profile", {"api_key": "must-not-store"})
    catalog.set_setting("llm_profile", {"provider": "local", "model": "extractive", "api_key_ref": "ENV_KEY"})
    assert catalog.get_setting("llm_profile")["provider"] == "local"


def test_admission_and_publication_maintenance_guards(catalog):
    catalog.set_maintenance(admissions_paused=True)
    with pytest.raises(CapacityError):
        submit(catalog)
    catalog.set_maintenance(admissions_paused=False)
    job, _ = submit(catalog)
    claim = catalog.claim(job.job_id, "worker")
    catalog.set_maintenance(activations_paused=True)
    assert not complete(catalog, claim)
    assert catalog.active_run_ids() == []
    catalog.set_maintenance(activations_paused=False)
    assert complete(catalog, claim)


def test_unfenced_repository_writes_are_rejected(catalog):
    job, _ = submit(catalog)
    job.complete("forged")
    with pytest.raises(ConflictError):
        catalog.job_repo.save(job)
    assert catalog.active_run_ids() == []


def test_catalog_reopen_keeps_jobs_outbox_and_configuration(catalog):
    job, _ = submit(catalog)
    url = catalog.engine.url.render_as_string(hide_password=False)
    catalog.set_setting("llm_profile", {"provider": "local"})
    other = Catalog(url, allow_sqlite=True)
    try:
        assert other.job_repo.get(job.job_id)
        assert other.ready_outbox()[0]["job_id"] == job.job_id
        assert other.get_setting("llm_profile") == {"provider": "local"}
    finally:
        other.close()


def test_sqlite_is_explicitly_test_only():
    with pytest.raises(ValidationError):
        Catalog("sqlite:///:memory:")


def test_shadow_build_is_not_reported_as_published_document(catalog):
    target = catalog.ensure_generation("shadow", "shadow_collection", {"provider": "local", "model": "shadow", "dimension": 8})
    job, _ = submit(catalog, generation=target)
    claim = catalog.claim(job.job_id, "builder")
    assert complete(catalog, claim)
    assert catalog.active_run_ids() == []
    assert catalog.document_repo.get(job.document_id).status == JobStatus.PENDING


def test_export_requires_maintenance_and_no_running_work(catalog):
    job, _ = submit(catalog)
    with pytest.raises(ConflictError):
        catalog.export_snapshot()
    claim = catalog.claim(job.job_id, "worker")
    catalog.set_maintenance(admissions_paused=True, activations_paused=True)
    with pytest.raises(ConflictError):
        catalog.export_snapshot()
    catalog.set_maintenance(activations_paused=False)
    assert complete(catalog, claim)
    catalog.set_maintenance(activations_paused=True)
    snapshot = catalog.export_snapshot()
    assert len(snapshot["tables"]["documents"]) == 1
    assert catalog.validate()["ok"]


def test_legacy_import_preserves_ids_and_requires_rebuild(catalog):
    generation = catalog.active_generation()
    rows = [{"document_id": "doc_123456789abc", "file_name": "legacy.pdf", "sha256": "b" * 64,
        "size_bytes": 100, "job_id": "job_123456789abc", "status": "completed"}]
    old_jobs = [{"job_id": "job_123456789abc", "status": "completed"}]
    with pytest.raises(ConflictError):
        catalog.import_legacy(rows, old_jobs, generation_id=generation["generation_id"], profile_id=generation["profile_id"])
    catalog.set_maintenance(admissions_paused=True, dispatch_paused=True, activations_paused=True)
    result = catalog.import_legacy(rows, old_jobs, generation_id=generation["generation_id"], profile_id=generation["profile_id"])
    assert result == {"imported": 1, "skipped": 0}
    assert catalog.import_legacy(rows, old_jobs, generation_id=generation["generation_id"], profile_id=generation["profile_id"]) == {"imported": 0, "skipped": 1}
    assert catalog.active_run_ids() == []
    assert catalog.document_repo.get(rows[0]["document_id"]).status == JobStatus.PENDING
    assert catalog.get_setting("legacy_import:job_123456789abc")["vectors_verified"] is False
    catalog.set_maintenance(admissions_paused=False, dispatch_paused=False, activations_paused=False)
    assert catalog.job_context(rows[0]["job_id"])["source_id"] == rows[0]["document_id"]
    assert len(catalog.ready_outbox()) == 1


def test_capacity_is_checked_before_creating_additional_jobs(catalog):
    catalog.max_pending_jobs = 1
    first, _ = submit(catalog)
    assert submit(catalog)[0].job_id == first.job_id
    with pytest.raises(CapacityError):
        submit(catalog, digest="b")
    assert len(catalog.document_repo.list()) == 1


def test_pipeline_upgrade_allows_new_revision_of_same_immutable_pdf(catalog):
    job, _ = submit(catalog)
    first = catalog.claim(job.job_id, "old-pipeline")
    assert complete(catalog, first)
    catalog.pipeline_version = "layout-v3"
    target = catalog.ensure_generation("pipeline-v3", "index_pipeline_v3", {
        "provider": "local", "model": "same-model", "dimension": 8, "pipeline_version": "layout-v3",
    })
    rebuilt, _ = submit(catalog, generation=target)
    second = catalog.claim(rebuilt.job_id, "new-pipeline")
    assert first["revision_id"] != second["revision_id"]
    assert complete(catalog, second)
    assert catalog.generation_manifest(target["generation_id"])["missing_document_ids"] == []
    catalog.set_maintenance(activations_paused=True)
    assert catalog.activate_generation(target["generation_id"])
    assert catalog.active_run_ids() == [second["run_id"]]


def test_garbage_candidates_exclude_every_published_or_live_run(catalog):
    job, _ = submit(catalog)
    published = catalog.claim(job.job_id, "published")
    assert complete(catalog, published)
    retry, _ = submit(catalog, force=True)
    failed = catalog.claim(retry.job_id, "failed")
    assert catalog.fail(retry.job_id, "failed", failed["epoch"], "fixture failure")
    another, _ = submit(catalog, force=True)
    live = catalog.claim(another.job_id, "live")
    rows = catalog.garbage_runs(retention_seconds=0)
    assert [row["run_id"] for row in rows] == [failed["run_id"]]
    assert published["run_id"] not in [row["run_id"] for row in rows]
    assert live["run_id"] not in [row["run_id"] for row in rows]

def test_cutover_changes_pending_pipeline_and_revision_consistently(catalog):
    job, _ = submit(catalog)
    old = catalog.job_context(job.job_id)
    target = catalog.ensure_generation("pipeline-v3", "chunks_v3", {
        "provider":"local", "model":"hash-v1", "dimension":8, "normalization":"l2",
        "pipeline_version":"layout-v3",
    })
    catalog.set_maintenance(activations_paused=True)
    catalog.activate_generation(target["generation_id"])
    superseded = catalog.job_context(job.job_id)
    new = catalog.job_context(superseded["replacement_job_id"])
    assert new["pipeline_version"] == "layout-v3"
    assert new["revision_id"] != old["revision_id"]
    assert new["source_id"] == old["source_id"]
    with catalog.engine.connect() as connection:
        assert connection.scalar(select(s.revisions.c.pipeline_version).where(
            s.revisions.c.revision_id == new["revision_id"])) == "layout-v3"
    catalog.pipeline_version = "layout-v3"
    catalog.set_maintenance(activations_paused=False)
    claim = catalog.claim(new["job_id"], "v3-worker")
    assert claim and complete(catalog, claim)


def test_delete_running_document_terminates_run_and_makes_it_collectable(catalog):
    job, _ = submit(catalog)
    claim = catalog.claim(job.job_id, "worker")
    catalog.document_repo.delete(job.document_id)
    with catalog.engine.connect() as connection:
        run = connection.execute(select(s.runs).where(s.runs.c.run_id == claim["run_id"])).mappings().one()
        assert run["status"] == "cancelled" and run["finished_at"] is not None
    assert not complete(catalog, claim)
    assert not catalog.heartbeat(job.job_id, "worker", claim["epoch"])
    assert claim["run_id"] in {r["run_id"] for r in catalog.garbage_runs(retention_seconds=0)}


def test_document_listing_uses_one_select_with_correct_publication_status(catalog):
    from sqlalchemy import event
    first, _ = submit(catalog, digest="a")
    second, _ = submit(catalog, digest="b")
    claim = catalog.claim(first.job_id, "worker")
    assert complete(catalog, claim)
    statements = []
    def record(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(catalog.engine, "before_cursor_execute", record)
    try:
        listed = {d.document_id:d for d in catalog.document_repo.list()}
    finally:
        event.remove(catalog.engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert listed[first.document_id].status == JobStatus.COMPLETED
    assert listed[first.document_id].chunk_count == 5
    assert listed[second.document_id].status == JobStatus.PENDING


def test_query_snapshot_filters_runs_and_pins_matching_profile(catalog):
    first, _ = submit(catalog, digest="a")
    second, _ = submit(catalog, digest="b")
    c1 = catalog.claim(first.job_id, "one")
    c2 = catalog.claim(second.job_id, "two")
    assert complete(catalog, c1) and complete(catalog, c2)
    snapshot = catalog.query_snapshot([first.document_id])
    assert snapshot["run_ids"] == [c1["run_id"]]
    assert snapshot["generation"]["profile_id"] == c1["profile_id"]
    assert catalog.query_snapshot([])["run_ids"] == []
