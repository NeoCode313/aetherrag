"""Generation construction, pinned queries and rollback through the real composition root."""
import io
import fitz

from app.cli import reindex, cutover, collect_failed_runs
from app.infrastructure.config import Settings
from app.presentation.dependencies import AppContainer


def test_dimension_migration_keeps_old_queries_and_supports_rollback(tmp_path):
    container = AppContainer(Settings(
        environment="test", data_dir=tmp_path, llm_provider="local", embedding_provider="local",
        auto_seed_sample_manual=False, vector_backend="memory", auth_enabled=False))
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "1. Operating pressure\nThe rated pressure for valve VX-12 is 14 bar.")
    job, _ = container.ingest_use_case.submit("manual.pdf", pdf.tobytes())
    pdf.close()
    assert container.ingest_use_case.execute_job(job.job_id).status.value == "completed"
    old = container.catalog.active_generation()
    old_query = container.query_use_case
    profile = dict(container.catalog.profile_by_id(old["profile_id"]))
    profile.update(dimension=32, profile_id="emb_test_dimension32")
    new = container.catalog.ensure_generation("fixture-dimension32", "fixture_dim32", profile)
    jobs = reindex(container, new["generation_id"])
    assert len(jobs) == 1
    assert container.ingest_use_case.execute_job(jobs[0]).status.value == "completed"
    assert container.catalog.active_generation()["generation_id"] == old["generation_id"]
    assert len(container.catalog.active_run_ids()) == 1
    cutover(container, new["generation_id"], old["generation_id"], resume=True)
    assert container.query_use_case.embedder.dimension == 32
    assert old_query.embedder.dimension == 768
    # An already-created query retains the original physical collection and run filter.
    assert old_query.vector_store.collection_name == old["collection"]
    assert old_query.vector_store.count_points() > 0
    assert container.query_use_case.vector_store.collection_name == new["collection"]
    cutover(container, old["generation_id"], new["generation_id"], resume=True)
    assert container.query_use_case.embedder.dimension == 768
    assert collect_failed_runs(container, apply=False) == []
    container.close()


def test_pipeline_parameters_are_versioned_and_resolved_from_immutable_profile(tmp_path):
    from app.infrastructure.providers import embedding_profile,digest
    settings=Settings(_env_file=None,environment="test",data_dir=tmp_path,llm_provider="local",
        embedding_provider="local",vector_backend="memory",auto_seed_sample_manual=False,auth_enabled=False)
    container=AppContainer(settings)
    try:
        previous=embedding_profile(settings)
        altered=settings.model_copy(update={"chunk_max_tokens":700,"chunk_overlap_ratio":.1})
        target_spec=embedding_profile(altered)
        assert digest(previous)!=digest(target_spec)
        target=container.catalog.ensure_generation(digest(target_spec),"different_chunking",target_spec)
        parser,chunker,linker=container.resolve_pipeline(target["profile_id"])
        assert chunker.max_tokens==700 and chunker.overlap_tokens==70
        assert container.chunker.max_tokens==1800
        with fitz.open() as pdf:
            page=pdf.new_page()
            page.insert_text((60,100),"Rated pressure for valve VX-12 is 14 bar.")
            job,_=container.ingest_use_case.submit_stream("manual.pdf",io.BytesIO(pdf.tobytes()),generation=target)
        assert container.ingest_use_case.execute_job(job.job_id).status.value=="completed"
        assert container.catalog.generation_manifest(target["generation_id"])["document_count"]==1
    finally:
        container.close()
