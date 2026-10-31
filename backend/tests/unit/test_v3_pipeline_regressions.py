"""Regression cases derived from the v2 audit, using independent PDF layouts."""
from unittest.mock import MagicMock

import httpx
import pymupdf
import pytest

from app.application.services.layout_chunker import LayoutAwareChunker, estimate_tokens
from app.application.services.spatial_linker import SpatialLinker, figure_label_from_caption
from app.application.use_cases.query_rag import contextualize_question
from app.domain.entities import BoundingBox, Chunk, ChunkKind, ConversationTurn, ElementKind, LayoutElement, ParsedDocument, ParsedPage, VisualAsset, VisualKind, SparseVector
from app.domain.exceptions import DependencyUnavailableError, ProviderError
from app.infrastructure.pdf.pymupdf_parser import PyMuPDFDocumentParser
from app.infrastructure.sample_manual import build_sample_manual_pdf, _fit_textbox
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.vector_store.qdrant_adapter import QdrantHybridVectorStore
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse


def test_numbered_operating_steps_are_indexed_even_without_a_heading(tmp_path):
    path = tmp_path / "steps.pdf"
    steps = ["1. Inspeccione la valvula V-12.", "2. Mantenga la presion inferior a 14.5 bar.", "3. Registre la medicion final."]
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for y, text in zip((100, 150, 200), steps):
            page.insert_text((50, y), text, fontsize=10)
        pdf.save(path)
    parsed = PyMuPDFDocumentParser(LocalFileStorage(tmp_path / "data"), crop_dpi=72, page_preview_dpi=72).parse(path, "run_steps", path.name)
    assert all(e.kind == ElementKind.PARAGRAPH for e in parsed.pages[0].elements)
    chunks = LayoutAwareChunker(max_tokens=600).chunk(parsed)
    assert all(text in "\n".join(c.text for c in chunks) for text in steps)


def test_scanned_body_with_long_native_header_still_requires_ocr():
    with pymupdf.open() as image_doc:
        page = image_doc.new_page(width=200, height=200)
        page.insert_text((10, 80), "SCANNED BODY")
        image = page.get_pixmap().tobytes("png")
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=400)
        page.insert_text((10, 30), "Native heading " * 5, fontsize=7)
        page.insert_image(pymupdf.Rect(0, 50, 300, 400), stream=image, keep_proportion=False)
        blocks = PyMuPDFDocumentParser._extract_raw_text_blocks(page, 300, 400)
        assert sum(len(b[1]) for b in blocks) > 40
        assert PyMuPDFDocumentParser._needs_ocr(page, blocks)


def test_oversized_header_never_leaves_short_values_without_their_column():
    text = "| Equipment identifier extended " + "x" * 170 + " | Pressure maximum permitted " + "y" * 170 + " |\n| --- | --- |\n| P-001 | 14.5 bar |\n| P-002 | 7.2 bar |"
    element = LayoutElement(1, ElementKind.TABLE, text, BoundingBox(.1,.1,.9,.9))
    doc = ParsedDocument("d", "m.pdf", [ParsedPage(1, 600, 800, [element], [])])
    chunks = LayoutAwareChunker(max_tokens=220).chunk(doc)
    assert all(estimate_tokens(c.embedding_text) <= 220 for c in chunks)
    for value, key in [("14.5 bar", "P-001"), ("7.2 bar", "P-002")]:
        values = [c.text for c in chunks if value in c.text]
        assert len(values) == 1
        assert "columna 2" in values[0] and "Pressure" in values[0] and key in values[0]


def test_configured_text_overlap_is_applied_and_progresses_within_budget():
    text = " ".join(f"token{i:03d}" for i in range(100))
    chunker = LayoutAwareChunker(max_tokens=150, overlap_ratio=.15)
    parts = chunker._split_long(text, "[m]", overlap=True)
    assert len(parts) > 3
    assert set(text.split()) == set(" ".join(parts).split())
    assert all(set(a.split()) & set(b.split()) for a,b in zip(parts, parts[1:]))
    assert all(estimate_tokens("[m]\n" + p) <= 150 for p in parts)
    plain = LayoutAwareChunker(max_tokens=150, overlap_ratio=0)._split_long(text, "[m]", overlap=True)
    assert not any(set(a.split()) & set(b.split()) for a,b in zip(plain, plain[1:]))


def test_zero_padded_figure_identity_matches_both_directions():
    assert figure_label_from_caption("Figura A.003: esquema") == "A.3"
    for caption, reference in [("Figura 02: esquema", "Ver Figura 2."), ("Figura 2: esquema", "Ver Figura 02.")]:
        visual = VisualAsset("v", "d", 2, BoundingBox(.1,.1,.9,.9), VisualKind.RASTER, "v.png", caption=caption, figure_label=figure_label_from_caption(caption))
        chunk = Chunk("c", "d", "m.pdf", 1, ChunkKind.TEXT, reference, BoundingBox(.1,.1,.9,.2))
        SpatialLinker().link([chunk],[visual])
        assert len(chunk.visuals) == 1


def test_multiturn_spanish_followup_retains_original_entity_and_stops_on_new_topic():
    history = [ConversationTurn("user", "Describe el sensor ST-104"), ConversationTurn("assistant", "Invented content E-999"), ConversationTurn("user", "¿Y su alarma?")]
    result = contextualize_question("¿Y su disparo?", history)
    assert "ST-104" in result and "su alarma" in result and "E-999" not in result
    assert contextualize_question("¿Y la presion de PT-301?", history) == "¿Y la presion de PT-301?"


def test_sample_pdf_contains_all_previous_silently_missing_content(tmp_path):
    path = build_sample_manual_pdf(tmp_path / "sample.pdf")
    with pymupdf.open(path) as pdf:
        pages = [" ".join(p.get_text().split()) for p in pdf]
        for index, term in [(0,"ASTM A516"),(1,"PLC"),(2,"Zona A/B"),(2,"PT-204 > 1.8 bar"),(2,"Confirmar entrada en servicio de bomba auxiliar PU-102B")]:
            assert term in pages[index]
        tables = pdf[2].find_tables().tables
        rows = [row for table in tables for row in table.extract()]
        assert all(all(str(cell or "").strip() for cell in row) for row in rows)


def test_sample_text_overflow_fails_loudly_instead_of_losing_content():
    with pymupdf.open() as doc:
        page = doc.new_page()
        with pytest.raises(ValueError, match="does not fit"):
            _fit_textbox(page, pymupdf.Rect(0,0,1,1), "Cannot fit", fontsize=6)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timeout"), ResponseHandlingException(httpx.ConnectError("down"))])
def test_qdrant_transport_failures_are_retryable_at_every_write_boundary(error):
    store = QdrantHybridVectorStore(in_memory=True)
    store.client = MagicMock()
    store.client.upsert.side_effect = error
    chunk = Chunk("c", "d", "m.pdf", 1, ChunkKind.TEXT, "test", BoundingBox(.1,.1,.9,.2))
    with pytest.raises(DependencyUnavailableError):
        store.upsert([chunk], [[1.,0.]], [SparseVector([1],[1.])])
    store.client.scroll.side_effect = error
    with pytest.raises(DependencyUnavailableError):
        store.verify_run("r", ["c"])
    store.client.query_points.side_effect = error
    store.client.get_collections.return_value.collections = [MagicMock(name="unused")]
    with pytest.raises(DependencyUnavailableError):
        store._call(store.client.query_points)


@pytest.mark.parametrize("code,retryable", [(400,False),(401,False),(429,True),(503,True)])
def test_qdrant_http_status_determines_retry_policy(code,retryable):
    store = QdrantHybridVectorStore(in_memory=True)
    operation = MagicMock(side_effect=UnexpectedResponse(code,"error",b"{}",httpx.Headers()))
    with pytest.raises(DependencyUnavailableError if retryable else ProviderError):
        store._call(operation)


def test_stack_guard_allows_explicit_proxy_without_allowing_ambient_cloud(monkeypatch):
    monkeypatch.setenv("STACK_TEST_URL","https://proxy:8443")
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200))) as client:
        assert client.get("https://proxy:8443").status_code == 200
        with pytest.raises(AssertionError,match="External HTTP"):
            client.get("https://api.openai.com")


def test_numbered_heading_keeps_depth_even_with_a_large_font():
    assert PyMuPDFDocumentParser._classify_block("2.3 Pressure",16,True,10)==(ElementKind.HEADING,2)
    assert PyMuPDFDocumentParser._classify_block("1. Inspect the valve",10,False,10)==(ElementKind.PARAGRAPH,0)
