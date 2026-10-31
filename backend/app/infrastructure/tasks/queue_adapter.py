"""Explicit task dispatch. PostgreSQL outbox is the source of truth."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from celery import Celery
from app.infrastructure.config import get_settings

_settings = get_settings()
celery_app = Celery("aetherrag", broker=_settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    enable_utc=True, task_track_started=True, worker_prefetch_multiplier=1,
    task_acks_late=True, task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True, task_publish_retry=False,
    broker_transport_options={"visibility_timeout": 2100},
    task_soft_time_limit=1800, task_time_limit=1860,
)


class ThreadedBackgroundTaskQueue:
    def __init__(self, runner, max_workers=2):
        self.runner = runner
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ingest")

    def enqueue_ingestion(self, job_id):
        self.executor.submit(self.runner, job_id)

    def close(self):
        self.executor.shutdown(wait=True)


class SynchronousTaskQueue:
    def __init__(self, runner):
        self.runner = runner

    def enqueue_ingestion(self, job_id):
        self.runner(job_id)


@celery_app.task(name="rag.execute_ingestion_job", acks_late=True, reject_on_worker_lost=True)
def execute_ingestion_job_task(job_id: str):
    from app.presentation.dependencies import get_container
    result = get_container().ingest_use_case.execute_job(job_id)
    return {"job_id": job_id, "status": result.status.value if result else "ignored"}


class CeleryTaskQueue:
    def enqueue_ingestion(self, job_id):
        # No fallback to local execution: a failed publish remains in the durable outbox.
        execute_ingestion_job_task.apply_async(args=[job_id], retry=False)
