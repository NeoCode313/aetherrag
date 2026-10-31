"""Unit tests for BoundingBox geometry and SpatialLinker text-to-diagram correlation."""

from __future__ import annotations

import pytest

from app.application.services.spatial_linker import (
    SpatialLinker,
    figure_number_from_caption,
    referenced_figure_numbers,
)
from app.domain.entities import (
    BoundingBox,
    Chunk,
    ChunkKind,
    ElementKind,
    LayoutElement,
    LinkReason,
    ParsedPage,
    VisualAsset,
    VisualKind,
)


def test_bounding_box_normalization_and_distance() -> None:
    b1 = BoundingBox.from_absolute(50, 100, 250, 200, page_width=500, page_height=1000)
    assert b1 == BoundingBox(0.1, 0.1, 0.5, 0.2)
    assert pytest.approx(b1.width) == 0.4
    assert pytest.approx(b1.height) == 0.1
    assert pytest.approx(b1.area) == 0.04

    # Box directly below b1 with a vertical gap of 0.05
    b2 = BoundingBox(0.1, 0.25, 0.5, 0.45)
    assert pytest.approx(b1.distance_to(b2), rel=1e-4) == 0.05
    assert b1.overlap_ratio(b2) == 0.0

    # Overlapping box has distance 0
    b3 = BoundingBox(0.2, 0.15, 0.4, 0.30)
    assert b1.distance_to(b3) == 0.0
    assert b1.overlap_ratio(b3) > 0.0


def test_caption_regex_and_reference_extraction() -> None:
    assert figure_number_from_caption("Figura 2: Corte del cojinete guía.") == 2
    assert figure_number_from_caption("Fig. 14 - Esquema eléctrico") == 14
    assert figure_number_from_caption("Tabla 1: Especificaciones") is None

    refs = referenced_figure_numbers("Consulte la Figura 1 y el Diagrama 3 (ver también fig. 2).")
    assert refs == {1, 2, 3}


def test_spatial_linker_assigns_closest_caption_and_links_nearest_diagram() -> None:
    fig_top = VisualAsset(
        asset_id="fig_p1_1",
        document_id="doc1",
        page=1,
        bbox=BoundingBox(0.1, 0.15, 0.9, 0.40),
        kind=VisualKind.RASTER,
        file_name="fig1.png",
    )
    fig_bottom = VisualAsset(
        asset_id="fig_p1_2",
        document_id="doc1",
        page=1,
        bbox=BoundingBox(0.1, 0.60, 0.9, 0.82),
        kind=VisualKind.VECTOR,
        file_name="fig2.png",
    )
    cap1 = LayoutElement(
        page=1,
        kind=ElementKind.CAPTION,
        text="Figura 1: Corte superior de la turbina.",
        bbox=BoundingBox(0.1, 0.41, 0.9, 0.44),
    )
    cap2 = LayoutElement(
        page=1,
        kind=ElementKind.CAPTION,
        text="Figura 2: Circuito inferior del regulador.",
        bbox=BoundingBox(0.1, 0.83, 0.9, 0.86),
    )
    page = ParsedPage(number=1, width=595, height=842, elements=[cap1, cap2], visuals=[fig_top, fig_bottom])

    linker = SpatialLinker(max_distance=0.25)
    linker.assign_captions(page)

    assert fig_top.figure_number == 1
    assert fig_bottom.figure_number == 2

    # Chunk physically close to fig_top (gap = 0.03) and far from fig_bottom (gap = 0.48)
    chunk_near_top = Chunk(
        chunk_id="c1",
        document_id="doc1",
        document_name="manual.pdf",
        page=1,
        kind=ChunkKind.TEXT,
        text="El distribuidor regula el caudal de entrada hacia el rodete.",
        bbox=BoundingBox(0.1, 0.06, 0.9, 0.12),
    )
    # Chunk on page 2 that explicitly cites "Figura 2"
    chunk_explicit_ref = Chunk(
        chunk_id="c2",
        document_id="doc1",
        document_name="manual.pdf",
        page=2,
        kind=ChunkKind.TEXT,
        text="El esquema completo de válvulas se detalla en la Figura 2.",
        bbox=BoundingBox(0.1, 0.1, 0.9, 0.2),
    )

    linker.link([chunk_near_top, chunk_explicit_ref], [fig_top, fig_bottom])

    assert chunk_near_top.nearest_visual is not None
    assert chunk_near_top.nearest_visual.asset.asset_id == "fig_p1_1"
    assert chunk_near_top.nearest_visual.reason == LinkReason.SPATIAL_PROXIMITY

    assert chunk_explicit_ref.nearest_visual is not None
    assert chunk_explicit_ref.nearest_visual.asset.asset_id == "fig_p1_2"
    assert chunk_explicit_ref.nearest_visual.reason == LinkReason.EXPLICIT_REFERENCE
