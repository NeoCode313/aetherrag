"""Regressions for real geometry, figure identities and bounded embedding inputs."""

from pathlib import Path

import pymupdf
import pytest

from app.application.services.layout_chunker import LayoutAwareChunker, estimate_tokens
from app.application.services.spatial_linker import (
    SpatialLinker, figure_number_from_caption, referenced_figure_numbers,
)
from app.domain.entities import (
    BoundingBox, Chunk, ChunkKind, ElementKind, LayoutElement, LinkReason,
    ParsedDocument, ParsedPage, VisualAsset, VisualKind,
)
from app.domain.exceptions import ValidationError
from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.sample_manual import _draw_table


class PDFStorage:
    def __init__(self, root: Path):
        self.root = root

    def page_image_path(self, document_id: str, page: int) -> Path:
        return self.root / document_id / f"page-{page}.png"

    def asset_path(self, document_id: str, name: str) -> Path:
        return self.root / document_id / name


def _image() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=100, height=100)
    page.draw_rect(page.rect, color=(1, 0, 0), fill=(1, 0, 0))
    result = page.get_pixmap().tobytes("png")
    doc.close()
    return result


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("cropped", [False, True])
def test_rotated_cropbox_geometry_matches_preview_and_visual_crop(tmp_path, rotation, cropped):
    source = tmp_path / "manual.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((100, 120), "Dato tecnico 21.4 bar", fontsize=12)
    page.insert_image(pymupdf.Rect(100, 200, 300, 400), stream=_image())
    if cropped:
        page.set_cropbox(pymupdf.Rect(50, 70, 550, 750))
    page.set_rotation(rotation)
    doc.save(source)
    doc.close()
    storage = PDFStorage(tmp_path / "assets")
    parsed = PyMuPDFDocumentParser(storage, crop_dpi=72, page_preview_dpi=72).parse(source, "run_test", "manual.pdf")
    result = parsed.pages[0]
    text = next(e for e in result.elements if "21.4" in e.text)
    visual = next(v for v in result.visuals if v.kind == VisualKind.RASTER)
    with pymupdf.open(source) as original:
        p = original[0]
        raw = next(b for b in p.get_text("dict")["blocks"] if b.get("type") == 0)
        expected = pymupdf.Rect(raw["bbox"]) * p.rotation_matrix
        assert text.bbox.to_list() == BoundingBox.from_absolute(*expected, p.rect.width, p.rect.height).to_list()
    preview = pymupdf.Pixmap(str(storage.page_image_path("run_test", 1)))
    cx, cy = visual.bbox.center
    red = preview.pixel(int(cx * preview.width), int(cy * preview.height))
    assert red[0] > 230 and red[1] < 20 and red[2] < 20
    crop = pymupdf.Pixmap(str(storage.asset_path("run_test", visual.file_name)))
    red = crop.pixel(crop.width // 2, crop.height // 2)
    assert red[0] > 230 and red[1] < 20 and red[2] < 20


def _visual(label, page, document_id="d", asset_id=None):
    return VisualAsset(
        asset_id=asset_id or f"fig-{page}-{label}", document_id=document_id, page=page,
        bbox=BoundingBox(.1, .5, .9, .8), kind=VisualKind.RASTER, file_name="f.png",
        caption=f"Figura {label}: esquema", figure_number=figure_number_from_caption(f"Figura {label}: esquema"),
    )


def _chunk(text, page=1):
    return Chunk("c", "d", "m.pdf", page, ChunkKind.TEXT, text, BoundingBox(.1, .1, .9, .2))


def test_complete_figure_identity_cross_page_and_non_physical_distance():
    assert referenced_figure_numbers("Figura 2.1, Figura 2.9, Fig. 2-14 y Figura A.3.") == {"2.1", "2.9", "2-14", "A.3"}
    a, b = _visual("2.1", 1), _visual("2.9", 2)
    chunk = _chunk("Consulte Figura 2.9 para la válvula de salida.")
    SpatialLinker(max_distance=.01).link([chunk], [a, b])
    assert len(chunk.visuals) == 1
    assert chunk.visuals[0].asset is b
    assert chunk.visuals[0].reason == LinkReason.EXPLICIT_REFERENCE
    assert chunk.visuals[0].distance is None


def test_ambiguous_cross_page_reference_is_not_guessed_or_cross_document_linked():
    chunk = _chunk("Ver Figura 2.9.")
    SpatialLinker(max_distance=.01).link([chunk], [_visual("2.9", 2), _visual("2.9", 3)])
    assert chunk.visuals == []
    SpatialLinker(max_distance=.01).link([chunk], [_visual("2.9", 2, document_id="other")])
    assert chunk.visuals == []


def _document(elements, name="manual.pdf", visuals=None):
    return ParsedDocument("d", name, [ParsedPage(1, 600, 800, elements, visuals or [])])


def _element(kind, text, y=.1):
    return LayoutElement(1, kind, text, BoundingBox(.1, y, .9, y + .05))


def test_orphan_figure_and_table_captions_are_preserved():
    doc = _document([
        _element(ElementKind.CAPTION, "Figura A.3: presión máxima 21.4 bar"),
        _element(ElementKind.CAPTION, "Tabla 2-1: tolerancias de seguridad", .2),
    ])
    text = "\n".join(c.text for c in LayoutAwareChunker().chunk(doc))
    assert "presión máxima 21.4 bar" in text
    assert "tolerancias de seguridad" in text


def test_long_unpunctuated_unicode_and_metadata_respect_complete_embedding_budget():
    paragraph = "válvula🔧" * 1000
    section = "1. " + "Sección muy larga " * 100
    heading = _element(ElementKind.HEADING, section)
    heading.heading_level = 1
    doc = _document([heading, _element(ElementKind.PARAGRAPH, paragraph, .3)], name="manual_" + "á" * 300 + ".pdf")
    chunks = LayoutAwareChunker(max_tokens=200, overlap_ratio=0).chunk(doc)
    assert len(chunks) > 10
    assert all(estimate_tokens(c.embedding_text) <= 200 for c in chunks)
    assert "".join(c.text for c in chunks if c.text.startswith("válvula") or "🔧" in c.text) == paragraph
    assert "Sección muy larga" in "\n".join(c.text for c in chunks)
    assert all(c.document_name == doc.file_name and c.section_path == (section.strip(),) for c in chunks)


def test_large_table_repeats_headers_and_preserves_rows_with_bounded_chunks():
    rows = [f"| E-{i:03} | Acción autorizada {i} |" for i in range(100)]
    table = "| Código | Acción |\n| --- | --- |\n" + "\n".join(rows)
    chunks = LayoutAwareChunker(max_tokens=220).chunk(_document([_element(ElementKind.TABLE, table)]))
    assert len(chunks) > 5
    assert all(c.kind == ChunkKind.TABLE and estimate_tokens(c.embedding_text) <= 220 for c in chunks)
    assert all(c.text.startswith("| Código | Acción |\n| --- | --- |") for c in chunks)
    combined = "\n".join(c.text for c in chunks)
    assert all(combined.count(row) == 1 for row in rows)


def test_oversized_table_cell_and_figure_description_are_bounded_and_preserved():
    value = "🛠válvula" * 400
    table = "| Código | Descripción |\n| --- | --- |\n| E-409 | " + value + " |"
    visual = _visual("A.3", 1)
    visual.description = value
    chunks = LayoutAwareChunker(max_tokens=200).chunk(_document([_element(ElementKind.TABLE, table)], visuals=[visual]))
    assert all(estimate_tokens(c.embedding_text) <= 200 for c in chunks)
    assert any("Fila 1, columna 2" in c.text for c in chunks)
    figures = [c for c in chunks if c.kind == ChunkKind.FIGURE]
    assert len(figures) > 1 and all(c.visuals[0].asset is visual for c in figures)
    assert "E-409" in "\n".join(c.text for c in chunks)


def _scan(path):
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_image(page.rect, stream=_image(), keep_proportion=False)
    doc.save(path)
    doc.close()


def test_full_page_scan_is_retained_and_optional_ocr_reports_missing_dependency(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    _scan(path)
    storage = PDFStorage(tmp_path / "assets")
    parsed = PyMuPDFDocumentParser(storage, crop_dpi=72, page_preview_dpi=72).parse(path, "run_scan", "scan.pdf")
    assert len(parsed.visuals) == 1 and parsed.visuals[0].bbox.area == pytest.approx(1)
    def unavailable(*args, **kwargs):
        raise RuntimeError("Tesseract not installed")
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", unavailable)
    with pytest.raises(ValidationError, match="Tesseract"):
        PyMuPDFDocumentParser(storage, crop_dpi=72, page_preview_dpi=72, ocr_enabled=True).parse(path, "run_ocr", "scan.pdf")


def test_pdf_page_pixel_and_time_limits_fail_explicitly(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    _scan(path)
    storage = PDFStorage(tmp_path / "assets")
    with pytest.raises(ValidationError, match="píxeles"):
        PyMuPDFDocumentParser(storage, max_render_pixels=100).parse(path, "run_big", "scan.pdf")
    doc = pymupdf.open(path)
    doc.new_page()
    multi = tmp_path / "multi.pdf"
    doc.save(multi)
    doc.close()
    with pytest.raises(ValidationError, match="1 páginas"):
        PyMuPDFDocumentParser(storage, max_pages=1).parse(multi, "run_pages", "multi.pdf")
    ticks = iter([0, 121])
    monkeypatch.setattr("app.infrastructure.pdf.pymupdf_parser.time.monotonic", lambda: next(ticks))
    with pytest.raises(ValidationError, match="tiempo máximo"):
        PyMuPDFDocumentParser(storage).parse(path, "run_slow", "scan.pdf")


def test_two_column_reading_keeps_each_column_before_next():
    elements = [
        LayoutElement(1, ElementKind.PARAGRAPH, name, BoundingBox(x, y, x + .35, y + .05))
        for name, x, y in [("left 1", .1, .1), ("right 1", .55, .1), ("left 2", .1, .2), ("right 2", .55, .2)]
    ]
    assert [e.text for e in PyMuPDFDocumentParser._reading_order(elements)] == ["left 1", "left 2", "right 1", "right 2"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_table_native_cell_extraction_preserves_decimals_and_codes(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        _draw_table(page, 80, 180, [180, 140], [
            ["Sensor", "Umbral"], ["ST-104", "75.0 C"], ["TRIP-TEMP-02", "85.0 C (> 3.0 s)"],
        ])
        page.set_cropbox(pymupdf.Rect(30, 40, 570, 760))
        page.set_rotation(rotation)
        tables = PyMuPDFDocumentParser._extract_tables(page, 1, page.rect.width, page.rect.height)
        assert len(tables) == 1
        text = tables[0].text
        assert "ST-104" in text and "TRIP-TEMP-02" in text
        assert "75.0 C" in text and "85.0 C (> 3.0 s)" in text
