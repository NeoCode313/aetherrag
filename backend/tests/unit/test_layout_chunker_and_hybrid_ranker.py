"""Unit tests for LayoutAwareChunker and HybridRanker (RRF)."""

from __future__ import annotations

from app.application.services.hybrid_ranker import HybridRanker
from app.application.services.layout_chunker import LayoutAwareChunker
from app.domain.entities import (
    BoundingBox,
    Chunk,
    ChunkKind,
    ElementKind,
    LayoutElement,
    ParsedDocument,
    ParsedPage,
    VisualAsset,
    VisualKind,
)


def test_layout_chunker_preserves_section_hierarchy_and_atomic_tables() -> None:
    elements = [
        LayoutElement(
            page=1,
            kind=ElementKind.HEADING,
            text="1. Sistema Hidráulico",
            bbox=BoundingBox(0.1, 0.05, 0.9, 0.09),
            heading_level=1,
        ),
        LayoutElement(
            page=1,
            kind=ElementKind.HEADING,
            text="1.1 Acumulador de Nitrógeno",
            bbox=BoundingBox(0.1, 0.10, 0.9, 0.13),
            heading_level=2,
        ),
        LayoutElement(
            page=1,
            kind=ElementKind.PARAGRAPH,
            text="El acumulador AC-301 opera a una presión nominal de 140 bar.",
            bbox=BoundingBox(0.1, 0.14, 0.9, 0.22),
        ),
        LayoutElement(
            page=1,
            kind=ElementKind.CAPTION,
            text="Tabla 1: Presiones de tarado del acumulador.",
            bbox=BoundingBox(0.1, 0.24, 0.9, 0.27),
        ),
        LayoutElement(
            page=1,
            kind=ElementKind.TABLE,
            text="| Código | Presión |\n| --- | --- |\n| E-409 | < 115 bar |",
            bbox=BoundingBox(0.1, 0.28, 0.9, 0.45),
        ),
    ]
    visual = VisualAsset(
        asset_id="fig_p1_1",
        document_id="doc1",
        page=1,
        bbox=BoundingBox(0.1, 0.50, 0.9, 0.80),
        kind=VisualKind.VECTOR,
        file_name="fig_p1_1.png",
        caption="Figura 1: Esquema AC-301.",
        figure_number=1,
    )
    doc = ParsedDocument(
        document_id="doc1",
        file_name="Manual.pdf",
        pages=[ParsedPage(number=1, width=595, height=842, elements=elements, visuals=[visual])],
    )

    chunker = LayoutAwareChunker(max_tokens=200)
    chunks = chunker.chunk(doc)

    kinds = [c.kind for c in chunks]
    assert ChunkKind.TEXT in kinds
    assert ChunkKind.TABLE in kinds
    assert ChunkKind.FIGURE in kinds

    text_chunk = next(c for c in chunks if "El acumulador" in c.text)
    assert text_chunk.section_path == ("1. Sistema Hidráulico", "1.1 Acumulador de Nitrógeno")
    assert "1. Sistema Hidráulico › 1.1 Acumulador de Nitrógeno" in text_chunk.embedding_text

    table_chunk = next(c for c in chunks if c.kind == ChunkKind.TABLE)
    assert "Tabla 1: Presiones de tarado" in table_chunk.text
    assert "| E-409 | < 115 bar |" in table_chunk.text


def test_hybrid_ranker_reciprocal_rank_fusion_and_exact_code_boost() -> None:
    c_semantic = Chunk(
        chunk_id="c_sem",
        document_id="d1",
        document_name="m.pdf",
        page=1,
        kind=ChunkKind.TEXT,
        text="Descripción general del sistema de control de presión en turbinas.",
        bbox=BoundingBox(0.1, 0.1, 0.9, 0.2),
    )
    c_exact_code = Chunk(
        chunk_id="c_code",
        document_id="d1",
        document_name="m.pdf",
        page=3,
        kind=ChunkKind.TABLE,
        text="El código E-409 indica baja presión en el acumulador AC-301 (< 115 bar).",
        bbox=BoundingBox(0.1, 0.5, 0.9, 0.7),
    )

    ranker = HybridRanker(rrf_k=60, dense_weight=0.6, sparse_weight=0.4, exact_code_boost=0.01)
    fused = ranker.fuse(
        dense_results=[(c_semantic, 0.82), (c_exact_code, 0.79)],
        sparse_results=[(c_exact_code, 0.95)],
        query="¿Qué significa el código E-409 en AC-301?",
        top_k=2,
    )

    # c_exact_code appears in BOTH dense and sparse rankings AND matches E-409 + AC-301
    assert fused[0].chunk.chunk_id == "c_code"
    assert fused[0].dense_rank == 2
    assert fused[0].sparse_rank == 1
    assert fused[0].score > fused[1].score
