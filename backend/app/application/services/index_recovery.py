"""Recover old index profiles into an isolated generation without relabelling or deleting sources."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from sqlalchemy import select, func, update as sql_update
from app.infrastructure import catalog_schema as schema
from app.domain.exceptions import ConflictError


def start_recovery(container):
    catalog = container.catalog
    if container.index_state()["compatible"]:
        return {"status": "completed", "message": "El índice ya es compatible"}
    token = uuid.uuid4().hex
    with catalog._tx() as connection:
        controls = catalog._control(connection, lock=True)
        old = catalog._setting(connection, "index_recovery") or {}
        if old.get("status") == "running" and old.get("lease_until", 0) > time.time():
            return {"status": "running", "message": "La recuperación ya está en curso"}
        running = connection.scalar(select(func.count()).select_from(schema.jobs).where(schema.jobs.c.status == "running"))
        if running:
            raise ConflictError("Espera a que terminen los trabajos activos antes de recuperar el índice")
        prior = old.get("prior") if old.get("status") == "running" else None
        prior = prior or {"admissions_paused": controls["admissions_paused"],
                         "activations_paused": controls["activations_paused"],
                         "dispatch_paused": catalog._setting(connection, "dispatch_paused") == {"paused": True}}
        if any(prior.values()):
            raise ConflictError("Finaliza el mantenimiento administrativo antes de recuperar el índice")
        connection.execute(sql_update(schema.control).where(schema.control.c.control_id == 1).values(admissions_paused=True))
        catalog._set_setting(connection, "index_recovery", {
            "status": "running", "id": token, "completed": 0, "lease_until": time.time()+120, "prior": prior})
    def update(**values):
        with catalog._tx() as connection:
            catalog._control(connection, lock=True)
            state = catalog._setting(connection, "index_recovery") or {}
            if state.get("id") != token:
                raise ConflictError("La recuperación perdió su autorización")
            state.update(values, lease_until=time.time()+120)
            catalog._set_setting(connection, "index_recovery", state)
    done = threading.Event()
    lost = threading.Event()
    def renew():
        while not done.wait(5):
            try:
                update()
            except Exception:
                lost.set()
                return
    def work():
        heartbeat = threading.Thread(target=renew, name="recovery-heartbeat", daemon=True)
        heartbeat.start()
        try:
            catalog.set_maintenance(admissions_paused=True, dispatch_paused=False, activations_paused=False)
            if catalog.engine.dialect.name == "sqlite":
                directory = container.identity.directory / "backups"
                directory.mkdir(exist_ok=True, mode=0o700)
                source = sqlite3.connect(catalog.engine.url.database)
                target = sqlite3.connect(directory / ("catalog-"+token+".sqlite"))
                try: source.backup(target)
                finally: target.close(); source.close()
            # Source files and old Qdrant collections are never removed or overwritten.
            target_generation = container.configured_generation
            documents = container.doc_repo.list()
            total = len(documents)
            update(total=total)
            # Validate the entire corpus before accepting reconstruction jobs.
            for document in documents:
                context = catalog.job_context(document.job_id)
                if not container.storage.upload_path(context["source_id"]).is_file():
                    raise ConflictError("Falta una fuente PDF; no se activó un índice incompleto")
            for number, document in enumerate(documents, 1):
                if lost.is_set() or container._stop.is_set():
                    raise ConflictError("Recuperación interrumpida")
                context = catalog.job_context(document.job_id)
                path = container.storage.upload_path(context["source_id"])
                if not path.is_file():
                    raise ConflictError("Falta una fuente PDF; no se activó un índice incompleto")
                with path.open("rb") as stream:
                    job, _ = container.ingest_use_case.submit_stream(document.file_name, stream,
                        generation=target_generation, administrative=True)
                deadline = time.monotonic()+300
                while True:
                    result = container.ingest_use_case.execute_job(job.job_id)
                    if result.status.value == "completed":
                        break
                    if result.status.value in {"failed", "cancelled"}:
                        raise ConflictError("Un documento no pudo reconstruirse; se conservaron las fuentes")
                    if time.monotonic() > deadline or container._stop.is_set() or lost.is_set():
                        raise ConflictError("Recuperación interrumpida; vuelve a intentarlo")
                    update(completed=number-1)
                    time.sleep(.5)
                update(completed=number)
            catalog.set_maintenance(admissions_paused=True, dispatch_paused=True, activations_paused=True)
            update()
            from app.cli import cutover
            cutover(container, target_generation["generation_id"], catalog.active_generation()["generation_id"])
            update(status="completed", message="Índice compatible y fuentes conservadas")
        except Exception:
            if not lost.is_set():
                update(status="failed", message="No se completó la recuperación. Las fuentes y el índice anterior se conservan.")
        finally:
            done.set()
            heartbeat.join(timeout=6)
            # A stale recovery cannot undo maintenance belonging to a newer owner.
            with catalog._tx() as connection:
                catalog._control(connection, lock=True)
                state = catalog._setting(connection, "index_recovery") or {}
                if state.get("id") == token:
                    connection.execute(sql_update(schema.control).where(schema.control.c.control_id == 1).values(
                        admissions_paused=prior["admissions_paused"], activations_paused=prior["activations_paused"]))
                    catalog._set_setting(connection, "dispatch_paused", {"paused": prior["dispatch_paused"]})
            container.notify_dispatcher()
    thread = threading.Thread(target=work, name="index-recovery", daemon=True)
    container._recovery_thread = thread
    thread.start()
    return {"status": "running", "message": "Reconstrucción iniciada; no cierres la aplicación"}
