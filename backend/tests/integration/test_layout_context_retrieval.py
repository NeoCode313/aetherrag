"""Explicit diagram/table requests retain layout context and publication scope."""
from app.domain.entities import (BoundingBox, Chunk, ChunkKind, LinkedVisual, LinkReason,
    SparseVector, VisualAsset, VisualKind)
from app.infrastructure.vector_store.qdrant_adapter import QdrantHybridVectorStore
import pytest


@pytest.mark.parametrize("question", [
    "¿Cuáles son los componentes de la Figura 1 y la potencia y el salto nominal de la Tabla 1?",
    "¿Cuál es la potencia y el salto nominal de la turbina en la Tabla 1?",
])
def test_repeated_figure_captions_do_not_discard_table_values_or_retrieval_scores(tmp_path, question):
    from app.application.services.hybrid_ranker import HybridRanker
    from app.application.services.prompt_builder import DefensivePromptBuilder
    from app.application.use_cases.query_rag import QueryRAGUseCase
    from app.infrastructure.llm.local_fallback import GroundedExtractiveLLMProvider
    from app.infrastructure.storage import LocalFileStorage
    from app.infrastructure.vector_store.sparse_bm25 import HashedLexicalEncoder

    class Embedder:
        def embed_query(self, _question):
            return [1.,0.,0.,0.]

    store = QdrantHybridVectorStore(collection_name="repeated_figures", in_memory=True)
    store.ensure_collection(4)
    bbox = BoundingBox(.1,.1,.9,.3)
    chunks = []
    for page in range(1,9):
        visual = VisualAsset(f"v-{page}", "run", page, bbox, VisualKind.VECTOR,
            f"v-{page}.png", caption="Figura 1: Componentes", figure_number=1)
        chunks.append(Chunk(f"figure-{page}", "doc", "manual.pdf", page, ChunkKind.FIGURE,
            "Figura 1: Componentes de la turbina: cámara espiral y rodete.", bbox,
            visuals=[LinkedVisual(visual,0,LinkReason.SELF)]))
    chunks.append(Chunk("table", "doc", "manual.pdf", 1, ChunkKind.TABLE,
        "Tabla 1: Especificaciones nominales de la turbina\n| Parámetro | Valor |\n| --- | --- |\n"
        "| Potencia nominal | 145 MW |\n| Salto nominal | 182 m |",bbox))
    sparse = HashedLexicalEncoder()
    try:
        store.view("repeated_figures",run_id="published").upsert(chunks,
            [[1.,0.,0.,0.]]*len(chunks),[sparse.encode_document(c.text) for c in chunks])
        query = QueryRAGUseCase(Embedder(),sparse,store.view("repeated_figures",active_run_ids=["published"]),
            HybridRanker(),DefensivePromptBuilder(),GroundedExtractiveLLMProvider(),LocalFileStorage(tmp_path))
        answer = query.execute(question,document_ids=["doc"])
        assert not answer.abstained
        assert "145 MW" in answer.answer and "182 m" in answer.answer
        assert answer.metrics.max_cosine_similarity > .9
        assert any(citation.chunk_id=="table" for citation in answer.citations)
    finally:
        store.client.close()


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
