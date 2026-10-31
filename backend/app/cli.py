"""Operator tools for staged index construction and controlled generation cutover."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from qdrant_client.http import models
from app.domain.exceptions import ConflictError
from app.presentation.dependencies import AppContainer


def reindex(container, generation_id: str) -> list[str]:
    target = container.catalog.generation_by_id(generation_id)
    if not target or target["status"] not in {"active", "building"}:
        raise ConflictError("La generación destino debe estar activa o en construcción")
    submitted = []
    for document in container.doc_repo.list():
        context = container.catalog.job_context(document.job_id)
        with container.storage.upload_path(context["source_id"]).open("rb") as stream:
            job, _ = container.ingest_use_case.submit_stream(
                document.file_name, stream, generation=target)
        submitted.append(job.job_id)
    return submitted


def cutover(container, generation_id: str, expected: str, *, resume=False) -> dict:
    catalog = container.catalog
    # A failed cutover deliberately leaves maintenance enabled for operator inspection.
    catalog.set_maintenance(admissions_paused=True, dispatch_paused=True)
    catalog.set_maintenance(activations_paused=True)
    manifest = catalog.generation_manifest(generation_id)
    if manifest["missing_document_ids"]:
        raise ConflictError("El destino no cubre el corpus; complete el delta antes del corte")
    target_store = container.store_for_generation(
        generation_id, active_run_ids=manifest["run_ids"])
    expected_points = sum(r["document_stats"]["chunk_count"] for r in manifest["runs"])
    if target_store.count_points() != expected_points:
        raise ConflictError("La cantidad de puntos del destino no coincide con su manifiesto")
    catalog.activate_generation(generation_id, expected_active_id=expected)
    if resume:
        catalog.set_maintenance(admissions_paused=False, dispatch_paused=False,
                                activations_paused=False)
    return {"active_generation": generation_id, "resumed": resume}


def collect_failed_runs(container, *, retention_seconds=86400, apply=False) -> list[dict]:
    catalog = container.catalog
    candidates = catalog.garbage_runs(retention_seconds=retention_seconds)
    if apply:
        state = catalog.maintenance()
        if not all(state.get(k) for k in ("admissions_paused", "activations_paused", "dispatch_paused")):
            raise ConflictError("La recolección requiere mantenimiento pausado")
        for item in candidates:
            store = container.store_for_generation(item["generation_id"])
            if store.client.collection_exists(store.collection_name):
                store.client.delete(store.collection_name,
                    models.FilterSelector(filter=models.Filter(must=[
                        models.FieldCondition(key="run_id", match=models.MatchValue(value=item["run_id"]))
                    ])), wait=True)
            container.storage.delete_document(item["run_id"])
            container.storage.manifest_path(item["run_id"]).unlink(missing_ok=True)
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generations")
    build = sub.add_parser("reindex")
    build.add_argument("generation_id")
    activate = sub.add_parser("cutover")
    activate.add_argument("generation_id")
    activate.add_argument("--expected", required=True)
    activate.add_argument("--resume", action="store_true")
    gc = sub.add_parser("gc")
    gc.add_argument("--retention-seconds", type=int, default=86400)
    gc.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    container = AppContainer()
    try:
        if args.command == "generations":
            result = container.catalog.list_generations()
        elif args.command == "reindex":
            result = {"job_ids": reindex(container, args.generation_id)}
        elif args.command == "cutover":
            result = cutover(container, args.generation_id, args.expected, resume=args.resume)
        else:
            result = {"apply": args.apply, "runs": collect_failed_runs(
                container, retention_seconds=args.retention_seconds, apply=args.apply)}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        container.close()


if __name__ == "__main__":
    main()

