"""Bounded, atomic on-demand previews of immutable, published PDF revisions.

PyMuPDF runs in a separate process, never concurrently in API threads. A cache
miss has a real wall-clock deadline; concurrent requests share the completed PNG.
"""
from __future__ import annotations

import math
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time

from app.domain.exceptions import DependencyUnavailableError, NotFoundError, ValidationError


def _render(source, page_number, target, dpi, max_pixels, pipe):
    try:
        import pymupdf
        with pymupdf.open(source) as document:
            if document.needs_pass or not 1 <= page_number <= len(document):
                pipe.send("missing")
                return
            page = document[page_number - 1]
            pixels = math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72)
            if pixels > max_pixels:
                pipe.send("budget")
                return
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            pixmap.save(target, output="png")
            pipe.send("ok")
    except Exception:
        # No PDF text, paths or decoder exception bodies cross the HTTP boundary.
        pipe.send("unavailable")
    finally:
        pipe.close()


class PagePreviewRenderer:
    def __init__(self, catalog, storage, *, timeout_seconds=20, max_pixels=20_000_000, dpi=135):
        self.catalog, self.storage = catalog, storage
        if not 1 <= timeout_seconds <= 120 or not 36 <= dpi <= 300 or max_pixels < 1:
            raise ValueError("Invalid preview resource limits")
        self.timeout_seconds, self.max_pixels, self.dpi = timeout_seconds, max_pixels, dpi
        self._slots = threading.BoundedSemaphore(1)
        self._locks = [threading.Lock() for _ in range(64)]

    def get(self, run_id: str, page_number: int) -> Path:
        # Bind the image to its original run/revision, including historical citations.
        # Authorization precedes cache lookup and creation of files or processes.
        source = self.catalog.published_run_source(run_id)
        if not source or not 1 <= page_number <= source["page_count"]:
            raise NotFoundError("La página no pertenece a una revisión publicada")
        target = self.storage.page_image_path(run_id, page_number)
        if target.is_file():
            return target
        deadline = time.monotonic() + self.timeout_seconds
        def remaining():
            return max(0.001, deadline - time.monotonic())
        lock = self._locks[hash((run_id, page_number)) % len(self._locks)]
        if not lock.acquire(timeout=remaining()):
            raise DependencyUnavailableError("La vista de página está ocupada; vuelve a intentarlo")
        try:
            if target.is_file():
                return target
            if not self._slots.acquire(timeout=remaining()):
                raise DependencyUnavailableError("La generación de vistas está ocupada; vuelve a intentarlo")
            try:
                return self._create(source["source_id"], page_number, target, remaining())
            finally:
                self._slots.release()
        finally:
            lock.release()

    def _create(self, source_id, page_number, target, timeout):
        source = self.storage.upload_path(source_id)
        if not source.is_file():
            raise NotFoundError("La fuente original de esta página no está disponible")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix="preview_", suffix=".png")
        os.close(descriptor)
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_render, args=(str(source), page_number, temporary,
            self.dpi, self.max_pixels, send), daemon=True)
        try:
            process.start()
            send.close()
            process.join(timeout)
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join()
                raise DependencyUnavailableError("La vista de página excedió el tiempo permitido")
            try:
                result = receive.recv() if receive.poll() else "unavailable"
            except EOFError:
                result = "unavailable"
            if result == "budget":
                raise ValidationError("La página excede el presupuesto de píxeles de previsualización")
            if result == "missing":
                raise NotFoundError("La página original no está disponible")
            if process.exitcode != 0 or result != "ok" or not Path(temporary).stat().st_size:
                raise DependencyUnavailableError("No se pudo generar la vista de página")
            os.replace(temporary, target)
            return target
        finally:
            receive.close()
            send.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join()
                process.close()
            Path(temporary).unlink(missing_ok=True)
