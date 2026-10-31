"""Explicit catalog maintenance; run with PYTHONPATH=backend.

DATABASE_URL is required. SQLite needs --allow-sqlite-test and is never a production
fallback. Export is metadata only: coordinate blob and Qdrant backups separately.
Legacy import defaults to a file/hash validation dry run; --apply requires maintenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from app.infrastructure.catalog import Catalog


def legacy_payload(state_dir: Path, source_root: Path) -> tuple[list[dict], list[dict]]:
    documents_path = state_dir / "documents.json"
    jobs_path = state_dir / "jobs.json"
    documents = list(json.loads(documents_path.read_text(encoding="utf-8")).values())
    jobs = list(json.loads(jobs_path.read_text(encoding="utf-8")).values()) if jobs_path.exists() else []
    root = source_root.resolve()
    for document in documents:
        source = (root / "uploads" / f"{document['document_id']}.pdf").resolve()
        if not source.is_relative_to(root / "uploads") or not source.is_file():
            raise ValueError(f"Missing or unsafe legacy source for {document['document_id']}")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != document["sha256"] or source.stat().st_size != document["size_bytes"]:
            raise ValueError(f"Legacy source/hash mismatch for {document['document_id']}")
    return documents, jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--allow-sqlite-test", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("status")
    commands.add_parser("validate")
    commands.add_parser("reconcile")
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    maintenance = commands.add_parser("maintenance")
    maintenance.add_argument("--mode", choices=("pause", "resume", "drain", "build"), required=True)
    imported = commands.add_parser("import-json")
    imported.add_argument("--state-dir", type=Path, required=True)
    imported.add_argument("--source-root", type=Path, required=True)
    imported.add_argument("--generation-id")
    imported.add_argument("--profile-id")
    imported.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = None
    if args.command == "import-json":
        payload = legacy_payload(args.state_dir, args.source_root)
        if not args.apply:
            print(json.dumps({"dry_run": True, "validated_documents": len(payload[0]),
                "legacy_jobs": len(payload[1]), "database_changed": False}, indent=2))
            return
        if not args.generation_id or not args.profile_id:
            parser.error("Import --apply requires --generation-id and --profile-id")
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    catalog = Catalog(args.database_url, allow_sqlite=args.allow_sqlite_test)
    try:
        if args.command in ("migrate", "status"):
            result = {"healthy": catalog.healthy(), "maintenance": catalog.maintenance(),
                "generations": catalog.list_generations(), "pending_jobs": catalog.pending_count()}
        elif args.command == "validate":
            result = catalog.validate()
        elif args.command == "reconcile":
            result = catalog.reconcile()
        elif args.command == "maintenance":
            # Drain stops new admissions/claims, but lets running workers publish.
            result = catalog.set_maintenance(admissions_paused=args.mode != "resume",
                dispatch_paused=args.mode in ("pause", "drain"), activations_paused=args.mode == "pause")
        elif args.command == "export":
            snapshot = catalog.export_snapshot()
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(snapshot, stream, indent=2, ensure_ascii=False)
            result = {"output": str(args.output.resolve()), "scope": "metadata only; not a coordinated data backup"}
        else:
            result = catalog.import_legacy(*payload, generation_id=args.generation_id, profile_id=args.profile_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "validate" and not result["ok"]:
            raise SystemExit(1)
    finally:
        catalog.close()


if __name__ == "__main__":
    main()
