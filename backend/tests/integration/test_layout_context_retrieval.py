"""Explicit diagram/table requests retain layout context and publication scope."""
from app.domain.entities import (BoundingBox, Chunk, ChunkKind, LinkedVisual, LinkReason,
    SparseVector, VisualAsset, VisualKind)
from app.infrastructure.vector_store.qdrant_adapter import QdrantHybridVectorStore


def test_layout_context_cannot_read_another_document_or_unpublished_run():
    store = QdrantHybridVectorStore(collection_name="layout_context", in_memory=True)
    store.ensure_collection(4)
    bbox = BoundingBox(.1,.1,.9,.3)
    visual = VisualAsset("figure", "doc-a", 1, bbox, VisualKind.VECTOR,
                         "figure.png", caption="Figura 1: Componentes", figure_number=1)
    figure = Chunk("figure", "doc-a", "a.pdf", 1, ChunkKind.FIGURE, "Figura 1: Componentes", bbox,
        visuals=[LinkedVisual(visual, 0, LinkReason.EXPLICIT_REFERENCE)])
    parts = [Chunk("table-"+str(i), "doc-a", "a.pdf", 1, ChunkKind.TABLE,
        "Tabla 1: Datos nominales\n| Parámetro | Valor |\n| --- | --- |\n"+row, bbox)
        for i, row in enumerate(["| Potencia | 145 MW |", "| Salto | 182 m |"])]
    other = Chunk("other", "doc-b", "b.pdf", 1, ChunkKind.TABLE,
        "Tabla 1: Datos nominales\n| Potencia | 900 MW |", bbox)
    staged = Chunk("pending", "doc-a", "a.pdf", 1, ChunkKind.TABLE,
        "Tabla 1: Datos nominales\n| Potencia | 800 MW |", bbox)
    try:
        published = [figure, *parts, other]
        store.view("layout_context", run_id="published").upsert(published,
            [[1.,0.,0.,0.]]*len(published), [SparseVector([],[])]*len(published))
        store.view("layout_context", run_id="pending").upsert([staged], [[1.,0.,0.,0.]], [SparseVector([],[])])
        active = store.view("layout_context", active_run_ids=["published"])
        assert [c.chunk_id for c in active.context_chunks(["doc-a"], figures_only=True, figure_numbers=(1,))] == ["figure"]
        assert {c.chunk_id for c in active.context_chunks(["doc-a"], table_numbers=(1,))} == {"table-0", "table-1"}
        assert active.context_chunks(["doc-a"], table_numbers=(2,)) == []
        assert active.context_chunks([], table_numbers=(1,)) == []
    finally:
        store.client.close()
