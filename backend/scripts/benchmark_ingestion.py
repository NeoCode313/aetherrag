"""Reproducible CPU-only ingestion benchmark; no catalog, queue or cloud calls.

The default corpus repeats the project's synthetic technical manual. Use the
same directory across comparisons to preserve exactly the same source bytes.
Run in an isolated container with explicit CPU/memory limits, not in the API.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import time

import pymupdf

from app.application.services.layout_chunker import LayoutAwareChunker
from app.application.services.spatial_linker import SpatialLinker
from app.infrastructure.llm.semantic_embedding import MultilingualE5EmbeddingProvider
from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.sample_manual import build_sample_manual_pdf
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.vector_store.sparse_bm25 import HashedLexicalEncoder


def synthetic_source(directory: Path, pages: int) -> Path:
    target = directory / f"manual-{pages}.pdf"
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            seed = build_sample_manual_pdf(Path(temporary) / "seed.pdf")
            with pymupdf.open(seed) as template, pymupdf.open() as document:
                for number in range(pages):
                    page = number % len(template)
                    document.insert_pdf(template, from_page=page, to_page=page)
                document.save(target)
    with pymupdf.open(target) as document:
        if len(document) != pages:
            raise ValueError("The existing synthetic source has a different page count")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=24)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--defer-previews", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.pages <= 100 or not 1 <= args.threads <= 4:
        parser.error("Use 1-100 synthetic pages and 1-4 inference threads")
    args.directory.mkdir(parents=True, exist_ok=True)
    source = synthetic_source(args.directory, args.pages)
    phases = {}

    def measured(name, action):
        start, cpu = time.perf_counter(), time.process_time()
        result = action()
        phases[name] = round((time.perf_counter() - start) * 1000, 2)
        print(json.dumps({"phase": name, "ms": phases[name],
                          "cpu_ms": round((time.process_time() - cpu) * 1000, 2)}), flush=True)
        return result

    embedder = measured("model_init", lambda: MultilingualE5EmbeddingProvider(
        Path(os.environ["EMBEDDING_MODEL_DIR"]), threads=args.threads))
    with tempfile.TemporaryDirectory(dir=args.directory) as temporary:
        storage = LocalFileStorage(Path(temporary) / "data")
        kwargs = {"max_parse_seconds": 600, "allow_large_vectors": True}
        if "defer_page_previews" in inspect.signature(PyMuPDFDocumentParser).parameters:
            kwargs["defer_page_previews"] = args.defer_previews
        elif args.defer_previews:
            raise ValueError("This revision does not support deferred page previews")
        pdf_parser = PyMuPDFDocumentParser(storage, **kwargs)
        document = measured("parse", lambda: pdf_parser.parse(source, "run_benchmark",
            "Manual_Turbina_HX900.pdf", on_progress=lambda fraction, _message:
                print(json.dumps({"phase": "parsing_progress", "fraction": round(fraction, 3)}), flush=True)))
        linker = SpatialLinker(max_distance=.38)
        measured("captions", lambda: [linker.assign_captions(page) for page in document.pages])
        chunker = LayoutAwareChunker(480, .15, token_counter=embedder.count_tokens)
        chunks = measured("chunking", lambda: chunker.chunk(document))
        measured("linking", lambda: linker.link(chunks, document.visuals))
        vectors = measured("embedding", lambda: embedder.embed_documents([c.embedding_text for c in chunks]))
        sparse = HashedLexicalEncoder()
        measured("sparse", lambda: [sparse.encode_document(c.embedding_text) for c in chunks])
        # Exclude run IDs, timing and deferred preview files from the semantic
        # fingerprint. Include literal text, geometry, associations AND crop pixels.
        snapshot = {"pages": [{"number": p.number, "size": [p.width, p.height],
            "elements": [[e.kind.value, e.text, e.bbox.to_list()] for e in p.elements]}
            for p in document.pages], "chunks": [[c.kind.value, c.embedding_text, c.bbox.to_list(),
            [v.asset.asset_id for v in c.visuals]] for c in chunks],
            "visuals": [[v.asset_id, v.page, v.caption, v.inner_text, v.bbox.to_list(),
                hashlib.sha256(storage.asset_path(v.document_id, v.file_name).read_bytes()).hexdigest()]
                for v in document.visuals]}
        result = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "pages": len(document.pages), "chunks": len(chunks), "visuals": len(document.visuals),
            "vectors": len(vectors), "threads": args.threads,
            "deferred_previews": args.defer_previews, "timings_ms": phases,
            "pipeline_ms": round(sum(v for k, v in phases.items() if k != "model_init"), 2),
            "integrity_sha256": hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        for key in ("memory.peak", "memory.swap.current", "cpu.stat"):
            path = Path("/sys/fs/cgroup") / key
            if path.is_file():
                result[key] = path.read_text().strip()
        if hasattr(document, "timings_ms"):
            result["parser_timings_ms"] = document.timings_ms
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
