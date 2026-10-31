"""Multimodal PDF parser implemented with PyMuPDF (``pymupdf``).

Extracts from every page:
1. Full-page rendered preview image (so the frontend can draw live bounding-box overlays).
2. Structured tables (converted to Markdown with normalized ``BoundingBox``).
3. Raster images AND vector schematic clusters (rendered to crisp PNG crops with a small
   context margin, plus any inner text callouts inside vector diagrams).
4. Layout text blocks classified into headings, captions (``Figura X`` / ``Tabla Y``),
   and paragraphs, skipping blocks already covered by a table or page headers/footers.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pymupdf

from app.application.services.spatial_linker import CAPTION_RE, TABLE_CAPTION_RE
from app.domain.entities import (
    BoundingBox,
    ElementKind,
    LayoutElement,
    ParsedDocument,
    ParsedPage,
    VisualAsset,
    VisualKind,
)
from app.domain.ports import DocumentParser, FileStorage, ProgressCallback
from app.domain.exceptions import ValidationError

logger = logging.getLogger(__name__)

NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}\.?|SECCI[ÓO]N\s+\d+|CAP[ÍI]TULO\s+\d+)\s+\S+", re.I)


class PyMuPDFDocumentParser(DocumentParser):
    """High-speed C-backed PDF layout, table, and diagram extractor."""

    def __init__(
        self,
        storage: FileStorage,
        crop_dpi: int = 180,
        page_preview_dpi: int = 135,
        min_visual_area_ratio: float = 0.012,
        max_visual_area_ratio: float = 0.88,
        ocr_enabled: bool = False,
        ocr_language: str = "eng",
        ocr_dpi: int = 150,
        max_pages: int = 500,
        max_render_pixels: int = 20_000_000,
        max_parse_seconds: float = 120,
        max_visuals_per_page: int = 32,
        max_text_chars: int = 2_000_000,
        max_total_render_pixels: int = 500_000_000,
    ) -> None:
        if any(dpi < 36 or dpi > 300 for dpi in (crop_dpi, page_preview_dpi, ocr_dpi)):
            raise ValueError("PDF rendering/OCR DPI must be between 36 and 300")
        if min(max_pages, max_render_pixels, max_visuals_per_page, max_text_chars, max_total_render_pixels) < 1 or max_parse_seconds <= 0:
            raise ValueError("PDF resource limits must be positive")
        self.storage = storage
        self.crop_dpi = crop_dpi
        self.page_preview_dpi = page_preview_dpi
        self.min_visual_area_ratio = min_visual_area_ratio
        self.max_visual_area_ratio = max_visual_area_ratio
        self.ocr_enabled = ocr_enabled
        self.ocr_language = ocr_language
        self.ocr_dpi = ocr_dpi
        self.max_pages = max_pages
        self.max_render_pixels = max_render_pixels
        self.max_parse_seconds = max_parse_seconds
        self.max_visuals_per_page = max_visuals_per_page
        self.max_text_chars = max_text_chars
        self.max_total_render_pixels = max_total_render_pixels

    def parse(
        self,
        pdf_path: Path,
        document_id: str,
        file_name: str,
        on_progress: ProgressCallback | None = None,
    ) -> ParsedDocument:
        started = time.monotonic()
        doc = pymupdf.open(str(pdf_path))
        total_pages = len(doc)
        parsed_pages: list[ParsedPage] = []
        rendered_pixels = 0
        text_chars = 0

        def check_time() -> None:
            if time.monotonic() - started > self.max_parse_seconds:
                raise ValidationError("El PDF excedió el tiempo máximo de extracción configurado.")

        def render(page: pymupdf.Page, dpi: int, clip: pymupdf.Rect | None = None) -> pymupdf.Pixmap:
            nonlocal rendered_pixels
            check_time()
            region = page.rect if clip is None else clip & page.rect
            pixels = self._pixel_count(region, dpi)
            if pixels > self.max_render_pixels or rendered_pixels + pixels > self.max_total_render_pixels:
                raise ValidationError("El PDF excede el presupuesto de píxeles de renderizado configurado.")
            rendered_pixels += pixels
            pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
            check_time()
            return pixmap

        try:
            if doc.needs_pass:
                raise ValidationError("El PDF está cifrado; se requiere una copia sin contraseña.")
            if total_pages < 1 or total_pages > self.max_pages:
                raise ValidationError(f"El PDF debe contener entre 1 y {self.max_pages} páginas.")
            body_font_size = self._estimate_body_font_size(doc)

            for idx in range(total_pages):
                check_time()
                page_num = idx + 1
                page = doc[idx]
                rect = page.rect
                pw, ph = float(rect.width), float(rect.height)

                if on_progress is not None:
                    on_progress(
                        idx / max(1, total_pages),
                        f"Extrayendo layout multimodal de página {page_num}/{total_pages}…",
                    )

                # 1. Render full page preview for interactive UI bounding-box inspection
                page_img_path = self.storage.page_image_path(document_id, page_num)
                if not page_img_path.exists():
                    page_img_path.parent.mkdir(parents=True, exist_ok=True)
                    pix = render(page, self.page_preview_dpi)
                    pix.save(str(page_img_path))
                    del pix

                # 2. Extract raw text blocks with font metrics
                raw_blocks = self._extract_raw_text_blocks(page, pw, ph)
                if self.ocr_enabled and self._needs_ocr(page, raw_blocks):
                    ocr_pixels = self._pixel_count(page.rect, self.ocr_dpi)
                    if ocr_pixels > self.max_render_pixels or rendered_pixels + ocr_pixels > self.max_total_render_pixels:
                        raise ValidationError("La página excede el presupuesto de píxeles para OCR.")
                    rendered_pixels += ocr_pixels
                    try:
                        textpage = page.get_textpage_ocr(language=self.ocr_language, dpi=self.ocr_dpi, full=True)
                    except Exception as exc:
                        raise ValidationError(
                            "El OCR solicitado no está disponible: instale Tesseract y los datos "
                            f"del idioma '{self.ocr_language}' (TESSDATA_PREFIX)."
                        ) from exc
                    raw_blocks = self._extract_raw_text_blocks(page, pw, ph, textpage=textpage)
                    check_time()
                text_chars += sum(len(block[1]) for block in raw_blocks)
                if text_chars > self.max_text_chars:
                    raise ValidationError("El PDF excede el límite de texto extraíble configurado.")

                # 3. Extract tables as Markdown + BoundingBoxes
                table_elements = self._extract_tables(page, page_num, pw, ph)
                table_boxes = [t.bbox for t in table_elements]

                # 4. Extract visual assets (embedded raster images + vector schematic clusters)
                visuals = self._extract_visuals(
                    page=page,
                    document_id=document_id,
                    page_num=page_num,
                    pw=pw,
                    ph=ph,
                    raw_blocks=raw_blocks,
                    table_boxes=table_boxes,
                    render=render,
                )

                # 5. Classify text blocks (headings, captions, paragraphs) avoiding table duplicates
                text_elements: list[LayoutElement] = []
                for b_bbox, b_text, max_size, is_bold in raw_blocks:
                    # Skip running headers/footers on multi-page docs
                    if (b_bbox.y1 < 0.045 or b_bbox.y0 > 0.955) and len(b_text) < 90:
                        continue
                    # Skip text that is inside a parsed table
                    if any(b_bbox.overlap_ratio(tb) > 0.60 for tb in table_boxes):
                        continue
                    kind, heading_level = self._classify_block(b_text, max_size, is_bold, body_font_size)
                    text_elements.append(
                        LayoutElement(
                            page=page_num,
                            kind=kind,
                            text=b_text,
                            bbox=b_bbox,
                            font_size=max_size,
                            heading_level=heading_level,
                        )
                    )

                # Merge text elements and tables in natural top-to-bottom reading order
                all_elements = self._reading_order(text_elements + table_elements)

                parsed_pages.append(
                    ParsedPage(
                        number=page_num,
                        width=pw,
                        height=ph,
                        elements=all_elements,
                        visuals=visuals,
                    )
                )

            if on_progress is not None:
                on_progress(1.0, f"Extracción PDF completada ({total_pages} páginas).")

            check_time()
            return ParsedDocument(
                document_id=document_id,
                file_name=file_name,
                pages=parsed_pages,
            )
        finally:
            doc.close()

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _pixel_count(rect: pymupdf.Rect, dpi: int) -> int:
        return math.ceil(rect.width * dpi / 72) * math.ceil(rect.height * dpi / 72)

    @staticmethod
    def _canonical_box(page: pymupdf.Page, rect: object, pw: float, ph: float) -> BoundingBox:
        """All stored geometry uses the visible, rotated CropBox (same as preview).

        PyMuPDF extraction coordinates are CropBox-relative and unrotated. Its
        rotation matrix maps them to the coordinate system used by get_pixmap.
        """
        visible = (pymupdf.Rect(rect) * page.rotation_matrix) & page.rect
        return BoundingBox.from_absolute(visible.x0, visible.y0, visible.x1, visible.y1, pw, ph)

    @staticmethod
    def _needs_ocr(page: pymupdf.Page, blocks: list[tuple[BoundingBox, str, float, bool]]) -> bool:
        # Native headers do not make a scanned body searchable. Inspect image
        # regions and how much usable native text actually covers those regions.
        native_chars = sum(len(b[1].strip()) for b in blocks)
        for image in page.get_images():
            for rect in page.get_image_rects(image[0]):
                box = PyMuPDFDocumentParser._canonical_box(page, rect, page.rect.width, page.rect.height)
                inside = [(b, text) for b, text, _, _ in blocks if b.overlap_ratio(box) > .5]
                covered = sum(b.area for b, _ in inside)
                chars = sum(len(text.strip()) for _, text in inside)
                sparse_overlay = covered < box.area * .20
                if (box.area >= .10 and native_chars < 40) or (box.area >= .30 and (chars < 40 or sparse_overlay)):
                    return True
        return False

    @staticmethod
    def _reading_order(elements: list[LayoutElement]) -> list[LayoutElement]:
        """Conservative two-column heuristic separated by full-width blocks.

        Irregular layouts still require a specialized layout model; ambiguous
        regions keep geometric order rather than inventing a column structure.
        """
        ordered = sorted(elements, key=lambda el: (el.bbox.y0, el.bbox.x0))
        result: list[LayoutElement] = []
        band: list[LayoutElement] = []

        def flush() -> None:
            left = [e for e in band if e.bbox.x1 <= 0.53]
            right = [e for e in band if e.bbox.x0 >= 0.47]
            if len(left) >= 2 and len(right) >= 2 and len(left) + len(right) == len(band):
                result.extend(sorted(left, key=lambda e: e.bbox.y0))
                result.extend(sorted(right, key=lambda e: e.bbox.y0))
            else:
                result.extend(band)
            band.clear()

        for element in ordered:
            if element.bbox.width > 0.62:
                flush()
                result.append(element)
            else:
                band.append(element)
        flush()
        return result

    @staticmethod
    def _estimate_body_font_size(doc: pymupdf.Document) -> float:
        sizes: list[float] = []
        for page in doc[: min(8, len(doc))]:
            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = (span.get("text") or "").strip()
                        if len(txt) >= 15:
                            sizes.append(float(span.get("size", 10.0)))
        return statistics.median(sizes) if sizes else 10.5

    @staticmethod
    def _extract_raw_text_blocks(
        page: pymupdf.Page, pw: float, ph: float, textpage: object | None = None
    ) -> list[tuple[BoundingBox, str, float, bool]]:
        data = page.get_text("dict", textpage=textpage, flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
        results: list[tuple[BoundingBox, str, float, bool]] = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines_txt: list[str] = []
            max_font = 0.0
            bold_chars = 0
            total_chars = 0
            for line in block.get("lines", []):
                span_parts: list[str] = []
                for span in line.get("spans", []):
                    t = span.get("text", "")
                    if not t:
                        continue
                    span_parts.append(t)
                    sz = float(span.get("size", 10.0))
                    max_font = max(max_font, sz)
                    flags = int(span.get("flags", 0))
                    font_name = (span.get("font") or "").lower()
                    is_span_bold = bool(flags & 16) or "bold" in font_name or "black" in font_name
                    total_chars += len(t)
                    if is_span_bold:
                        bold_chars += len(t)
                line_str = "".join(span_parts).strip()
                if line_str:
                    lines_txt.append(line_str)
            full_text = "\n".join(lines_txt).strip()
            if not full_text:
                continue
            x0, y0, x1, y1 = block.get("bbox", (0, 0, pw, ph))
            bbox = PyMuPDFDocumentParser._canonical_box(page, (x0, y0, x1, y1), pw, ph)
            is_bold = total_chars > 0 and (bold_chars / total_chars) >= 0.5
            results.append((bbox, full_text, max_font, is_bold))
        return results

    @staticmethod
    def _classify_block(
        text: str, font_size: float, is_bold: bool, body_size: float
    ) -> tuple[ElementKind, int]:
        first_line = text.splitlines()[0].strip()
        if CAPTION_RE.match(first_line) or TABLE_CAPTION_RE.match(first_line):
            return ElementKind.CAPTION, 0

        is_short = len(text) <= 130 and "\n" not in text.strip()
        if is_short:
            # Numbering also marks maintenance instructions. Require visual
            # title evidence before using numbering to determine hierarchy.
            numbered = re.match(r"^\s*(\d+(?:\.\d+){0,5})\.?\s+\S", first_line)
            if font_size >= body_size * 1.14 or (is_bold and numbered):
                level = min(6, numbered.group(1).count(".") + 1) if numbered else (1 if font_size >= body_size * 1.32 else 2)
                return ElementKind.HEADING, level

        return ElementKind.PARAGRAPH, 0

    @staticmethod
    def _extract_tables(page: pymupdf.Page, page_num: int, pw: float, ph: float) -> list[LayoutElement]:
        # TableFinder's glyph reconstruction/rotation handling differs from
        # native text extraction. Detect cells on an unrotated page, read native
        # text there, then restore rotation before converting geometry.
        original_rotation = page.rotation
        try:
            if original_rotation:
                page.set_rotation(0)
            extracted = PyMuPDFDocumentParser._extract_table_cells(page)
        finally:
            if original_rotation:
                page.set_rotation(original_rotation)
        elements: list[LayoutElement] = []
        for rows, rectangle in extracted:
            cleaned_rows: list[list[str]] = []
            for row in rows:
                if not row:
                    continue
                cells = [re.sub(r"\s+", " ", (cell or "").strip()) for cell in row]
                if any(cells):
                    cleaned_rows.append(cells)
            if len(cleaned_rows) < 2 or len(cleaned_rows[0]) < 2:
                continue
            elements.append(LayoutElement(
                page=page_num, kind=ElementKind.TABLE,
                text=PyMuPDFDocumentParser._rows_to_markdown(cleaned_rows),
                bbox=PyMuPDFDocumentParser._canonical_box(page, rectangle, pw, ph),
            ))
        return elements

    @staticmethod
    def _extract_table_cells(page: pymupdf.Page) -> list[tuple[list[list[str]], tuple[float, ...]]]:
        try:
            finder = page.find_tables()
            tables = getattr(finder, "tables", []) or []
        except Exception as exc:
            logger.debug("Table detection skipped on page %d: %s", page.number + 1, exc)
            return []
        extracted = []
        for tbl in tables:
            try:
                # Use the detected cell geometry with MuPDF's native text order.
                # TableFinder.extract() reconstructs glyphs independently and can
                # move decimal points to another line (75.0 -> '750\n.'). Never
                # repair such numbers by guessing after the fact.
                cell_rows = getattr(tbl, "rows", None)
                rows = [
                    [page.get_text("text", clip=pymupdf.Rect(cell), sort=True).strip() if cell else "" for cell in row.cells]
                    for row in cell_rows
                ] if cell_rows else tbl.extract()
            except Exception:
                continue
            if not rows or len(rows) < 2:
                continue
            extracted.append((rows, tuple(tbl.bbox)))
        return extracted

    @staticmethod
    def _rows_to_markdown(rows: list[list[str]]) -> str:
        col_count = max(len(r) for r in rows)
        norm = [[c.replace("|", "\\|") for c in r] + [""] * (col_count - len(r)) for r in rows]
        header = "| " + " | ".join(norm[0]) + " |"
        sep = "| " + " | ".join(["---"] * col_count) + " |"
        body = ["| " + " | ".join(r) + " |" for r in norm[1:]]
        return "\n".join([header, sep, *body])

    def _extract_visuals(
        self,
        page: pymupdf.Page,
        document_id: str,
        page_num: int,
        pw: float,
        ph: float,
        raw_blocks: list[tuple[BoundingBox, str, float, bool]],
        table_boxes: list[BoundingBox],
        render: Callable[..., pymupdf.Pixmap] | None = None,
    ) -> list[VisualAsset]:
        candidates: list[tuple[BoundingBox, VisualKind]] = []

        # A. Embedded raster images
        seen_xrefs: set[int] = set()
        for img_info in page.get_images(full=True):
            xref = int(img_info[0])
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects:
                b = self._canonical_box(page, r, pw, ph)
                if self._is_valid_visual_box(b, table_boxes, allow_full_page=True):
                    candidates.append((b, VisualKind.RASTER))

        # B. Vector drawings (schematics, flowcharts, CAD diagrams)
        try:
            clusters = page.cluster_drawings() or []
        except Exception:
            clusters = []
        for crect in clusters:
            b = self._canonical_box(page, crect, pw, ph)
            if not self._is_valid_visual_box(b, table_boxes):
                continue
            # Skip vector boxes that are mostly covered by an already-detected raster image or table
            if any(b.overlap_ratio(existing_b) > 0.5 or existing_b.overlap_ratio(b) > 0.5 for existing_b, _ in candidates):
                continue
            candidates.append((b, VisualKind.VECTOR))

        # Sort top-to-bottom, left-to-right
        candidates.sort(key=lambda item: (round(item[0].y0, 3), round(item[0].x0, 3)))
        if len(candidates) > self.max_visuals_per_page:
            raise ValidationError(f"La página {page_num} excede el límite de {self.max_visuals_per_page} imágenes.")

        visuals: list[VisualAsset] = []
        for v_idx, (bbox, kind) in enumerate(candidates, start=1):
            asset_id = f"fig_p{page_num}_{v_idx}"
            file_name = f"{asset_id}.png"
            target_path = self.storage.asset_path(document_id, file_name)
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Slight padding (1.2%) around the box so schematic borders & callout numbers aren't clipped
            pad_x = pw * 0.012
            pad_y = ph * 0.012
            clip_rect = pymupdf.Rect(
                max(0.0, bbox.x0 * pw - pad_x),
                max(0.0, bbox.y0 * ph - pad_y),
                min(pw, bbox.x1 * pw + pad_x),
                min(ph, bbox.y1 * ph + pad_y),
            )
            if render is None:
                if self._pixel_count(clip_rect, self.crop_dpi) > self.max_render_pixels:
                    raise ValidationError("El recorte excede el presupuesto de píxeles configurado.")
                pix = page.get_pixmap(clip=clip_rect, dpi=self.crop_dpi, alpha=False)
            else:
                pix = render(page, self.crop_dpi, clip_rect)
            pix.save(str(target_path))

            # Collect any labels drawn inside the visual region
            inner_labels = [
                re.sub(r"\s+", " ", txt.strip())
                for b_box, txt, _sz, _bold in raw_blocks
                if b_box.overlap_ratio(bbox) > 0.55 and len(txt.strip()) <= 80
            ]

            visuals.append(
                VisualAsset(
                    asset_id=asset_id,
                    document_id=document_id,
                    page=page_num,
                    bbox=bbox,
                    kind=kind,
                    file_name=file_name,
                    inner_text=" · ".join(inner_labels),
                    width_px=pix.width,
                    height_px=pix.height,
                )
            )

        return visuals

    def _is_valid_visual_box(self, bbox: BoundingBox, table_boxes: list[BoundingBox], allow_full_page: bool = False) -> bool:
        if bbox.width < 0.08 or bbox.height < 0.06:
            return False
        if bbox.area < self.min_visual_area_ratio or (not allow_full_page and bbox.area > self.max_visual_area_ratio):
            return False
        # Skip decorative header/footer banners
        if bbox.y1 < 0.08 or bbox.y0 > 0.92:
            return False
        # Skip boxes that are actually table grid borders
        if any(bbox.overlap_ratio(tb) > 0.65 and tb.overlap_ratio(bbox) > 0.65 for tb in table_boxes):
            return False
        return True
