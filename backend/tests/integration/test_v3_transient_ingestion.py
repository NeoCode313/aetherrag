import pymupdf
import httpx
from sqlalchemy import update, select
from app.infrastructure import catalog_schema as s
from app.infrastructure.config import Settings
from app.presentation.dependencies import AppContainer


def test_actual_qdrant_timeout_enters_retry_wait_and_second_attempt_publishes(tmp_path,monkeypatch):
    container=AppContainer(Settings(environment="test",data_dir=tmp_path,llm_provider="local",
        embedding_provider="local",vector_backend="memory",auth_enabled=False,auto_seed_sample_manual=False))
    try:
        with pymupdf.open() as pdf:
            page=pdf.new_page()
            page.insert_text((60,100),"The nominal pressure for VX-12 is 14 bar.")
            job,_=container.ingest_use_case.submit("manual.pdf",pdf.tobytes())
        original=container.vector_store.client.upsert
        calls=[]
        def intermittent(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:
                raise httpx.ReadTimeout("Injected transport timeout")
            return original(*args,**kwargs)
        monkeypatch.setattr(container.vector_store.client,"upsert",intermittent)
        container.ingest_use_case.execute_job(job.job_id)
        with container.catalog.engine.begin() as connection:
            row=connection.execute(select(s.jobs).where(s.jobs.c.job_id==job.job_id)).mappings().one()
            assert row["status"]=="retry_wait" and row["attempt_count"]==1
            connection.execute(update(s.jobs).where(s.jobs.c.job_id==job.job_id).values(next_attempt_at=0))
        assert container.catalog.active_run_ids()==[]
        container.catalog.reconcile()
        assert container.ingest_use_case.execute_job(job.job_id).status.value=="completed"
        assert len(container.catalog.active_run_ids())==1
        assert len(container.catalog.garbage_runs(retention_seconds=0))==1
    finally:
        container.close()
