"""Hybrid retrieval with explicit evidence and capability-aware image attachments."""
from __future__ import annotations

import logging
import re
import time
import uuid
import unicodedata
from collections.abc import Sequence

from app.application.services.hybrid_ranker import HybridRanker, extract_technical_codes, normalize_technical_text
from app.application.services.prompt_builder import DefensivePromptBuilder
from app.application.services.spatial_linker import referenced_figure_numbers, FIGURE_ID, ID_END, figure_number_from_caption
from app.domain.entities import ConversationTurn, ImageInput, RAGAnswer, ScoredChunk
from app.domain.exceptions import ProviderError, ValidationError
from app.domain.ports import EmbeddingProvider, FileStorage, LLMProvider, SparseEncoder, VectorStore

logger = logging.getLogger(__name__)


def contextualize_question(question: str, history: Sequence[ConversationTurn]) -> str:
    """Carry explicit user context through follow-ups; assistant text is never evidence."""
    def followup(text: str) -> bool:
        text = text.strip().lstrip("¿¡?! ")
        # A standalone request can contain "su" while naming a completely new topic.
        # Do not contaminate it with a question from the previous PDF.
        if re.match(r"(?:puedes?|podrias?|explica(?:me)?|describe|define|que (?:es|son)|what (?:is|are)|explain)\b", text, re.I):
            return False
        return len(text) <= 180 and bool(re.search(
            r"\b(su|sus|ese|esa|esto|ello|its|that|this)\b|^(?:y|and)\b", text.lower()))
    if not followup(question):
        return question
    previous = [t.content.strip() for t in history if t.role == "user" and t.content.strip()]
    if not previous:
        return question
    # Explicit identifiers in a recent follow-up can establish a new topic.
    context = []
    for text in reversed(previous[-12:]):
        context.append(text)
        if extract_technical_codes(text) or not followup(text):
            break
    context.reverse()
    anchor = "\n".join(context)
    new_codes = set(extract_technical_codes(question))
    if new_codes and not new_codes.issubset(set(extract_technical_codes(anchor))):
        return question
    return f"Contexto de preguntas anteriores: {anchor[:1200]}\nSeguimiento: {question}"


class QueryRAGUseCase:
    def __init__(self, embedder: EmbeddingProvider, sparse_encoder: SparseEncoder,
                 vector_store: VectorStore, ranker: HybridRanker,
                 prompt_builder: DefensivePromptBuilder, llm: LLMProvider,
                 storage: FileStorage, top_k: int = 6, candidate_pool: int = 16,
                 min_dense_score: float = 0.28, attach_images_to_llm: bool = True,
                 max_attached_images: int = 2) -> None:
        self.embedder, self.sparse_encoder, self.vector_store = embedder, sparse_encoder, vector_store
        self.ranker, self.prompt_builder, self.llm, self.storage = ranker, prompt_builder, llm, storage
        self.top_k, self.candidate_pool = top_k, candidate_pool
        self.min_dense_score = min_dense_score
        self.attach_images_to_llm = attach_images_to_llm
        self.max_attached_images = max_attached_images

    def execute(self, question: str, history: Sequence[ConversationTurn] = (),
                document_ids: Sequence[str] | None = None, top_k: int | None = None) -> RAGAnswer:
        clean_q = (question or "").strip()
        if not clean_q or len(clean_q) > 2500:
            raise ValidationError("La consulta debe contener entre 1 y 2500 caracteres.")
        k = self.top_k if top_k is None else top_k
        if not 1 <= k <= 20:
            raise ValidationError("top_k debe estar entre 1 y 20.")
        t_start = time.perf_counter()
        contextual_q = contextualize_question(clean_q, history)
        query_dense = self.embedder.embed_query(contextual_q)
        query_sparse = self.sparse_encoder.encode_query(contextual_q)
        pool = max(self.candidate_pool, k * 2)
        # The injected store captures the authorized active generation for this request.
        dense = self.vector_store.dense_search(query_dense, limit=pool, document_ids=document_ids)
        sparse = self.vector_store.sparse_search(query_sparse, limit=pool, document_ids=document_ids)
        # Retain the bounded candidate union so layout enrichment can reuse real
        # retrieval scores even when a requested table was outside the first k.
        ranked = self.ranker.fuse(dense, sparse, query=contextual_q, top_k=pool * 2)
        scored = ranked[:k]
        normalized_q = "".join(c for c in unicodedata.normalize("NFD", clean_q.lower()) if not unicodedata.combining(c))
        overview = bool(re.search(r"de que trata|resumen|resume\b|resumir|objetivo.*documento|overview|summari[sz]e", normalized_q))
        figure_ids = referenced_figure_numbers(clean_q)
        table_ids = {figure_number_from_caption("Figura " + value) for value in re.findall(
            rf"\b(?:tabla|table)\s*({FIGURE_ID}){ID_END}", clean_q, re.I)}
        pages = [int(value) for value in re.findall(r"(?:pagina|page)\s+(\d{1,4})", normalized_q)]
        if hasattr(self.vector_store, "context_chunks") and (overview or figure_ids or table_ids or pages):
            scope = document_ids or list(dict.fromkeys(item.chunk.document_id for item in scored))
            groups = []
            if overview or (pages and not figure_ids and not table_ids):
                groups.append(self.vector_store.context_chunks(scope,
                    pages=[1] if overview else pages, limit=k) or [])
            # One group per requested label prevents repeated captions from
            # starving a different figure/table. A table-only request never
            # injects unrelated figures. Keep all reads under the store's scope.
            for number in sorted(figure_ids, key=str)[:k]:
                groups.append(self.vector_store.context_chunks(scope, pages=pages,
                    figures_only=True, figure_numbers=(number,), limit=k) or [])
            for number in sorted(table_ids, key=str)[:k]:
                groups.append(self.vector_store.context_chunks(scope, pages=pages,
                    table_numbers=(number,), limit=k) or [])
            by_id = {item.chunk.chunk_id: item for item in ranked}
            selected, seen = [], set()

            def add(item):
                if len(selected) < k and item.chunk.chunk_id not in seen:
                    selected.append(item)
                    seen.add(item.chunk.chunk_id)

            # Preserve the strongest hybrid hit and its score for the relevance
            # gate, then cover requested groups before filling by hybrid rank.
            for item in ranked[:1]:
                add(item)
            for group in groups:
                if group:
                    chunk = group[0]
                    add(by_id.get(chunk.chunk_id) or ScoredChunk(chunk, score=0.0))
            for item in ranked:
                add(item)
            for group in groups:
                for chunk in group:
                    add(by_id.get(chunk.chunk_id) or ScoredChunk(chunk, score=0.0))
            scored = selected
        source_preview = [{"document": item.chunk.document_name, "page": item.chunk.page,
                           "snippet": item.chunk.text[:400]} for item in scored[:3]]
        retrieval_ms = int((time.perf_counter() - t_start) * 1000)
        best_dense = max((sc.dense_score for sc in scored if sc.dense_score is not None), default=0.0)
        codes = extract_technical_codes(contextual_q)
        exact_identifier_hit = bool(codes) and any(all(re.search(
            r"(?<![\w/-])" + re.escape(code) + r"(?![\w/-])", normalize_technical_text(sc.chunk.text).lower())
            for code in codes) for sc in scored)
        # Lexical overlap is a retrieval signal, not proof that the requested property is present.
        candidates = self.prompt_builder.collect_visuals(scored)
        visual_question = bool(figure_ids or re.search(r"imagen|diagrama|esquema|flecha|linea|color|trazo|conect|figure|diagram|arrow|colour|color", normalized_q))
        can_inspect = bool(candidates) and visual_question and bool(getattr(self.llm, "supports_images", False))
        if not scored or (best_dense < self.min_dense_score and not exact_identifier_hit and not can_inspect and not overview):
            elapsed = int((time.perf_counter() - t_start) * 1000)
            return RAGAnswer(answer="No cuento con evidencia suficiente para responder. "
                             "Reformula la consulta o selecciona el manual pertinente.",
                abstained=True, abstention_reason="No se recuperó contexto con relevancia suficiente.",
                evidence_coverage="none", citations=[], primary_visual=None, related_visuals=[],
                retrieved=len(scored), model=self.llm.model_name, latency_ms=elapsed,
                timings_ms={"retrieval_ms": retrieval_ms, "generation_ms": 0, "total_ms": elapsed},
                answer_mode="extractive_evidence" if not getattr(self.llm, "supports_images", False)
                            and self.llm.model_name.startswith("local-") else "grounded_answer",
                message_id=uuid.uuid4().hex,
                metadata={"semantic_support": "not_assessed", "retrieval_question": contextual_q,
                          "abstention_stage": "retrieval", "retrieved_sources": source_preview})
        loaded: dict[str, tuple[bytes, str]] = {}
        supports_images = bool(getattr(self.llm, "supports_images", False))
        if self.attach_images_to_llm and supports_images:
            ordered = sorted(candidates.items(), key=lambda entry: (
                0 if entry[1].asset.figure_number in figure_ids else 1,
                0 if entry[1].reason.value == "explicit_reference" else 1))
            for aid, visual in ordered:
                if len(loaded) >= self.max_attached_images:
                    break
                path = self.storage.asset_path(visual.asset.document_id, visual.asset.file_name)
                try:
                    # Bound input size independently of the number of figures.
                    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                        continue
                    data = path.read_bytes()
                    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                    loaded[aid] = (data, mime)
                except OSError:
                    logger.warning("Could not attach visual asset %s", aid)
        bundle = self.prompt_builder.build(contextual_q, scored, history=history,
            max_visuals=self.max_attached_images, sent_asset_ids=list(loaded))
        images = [ImageInput(alias=alias, data=loaded[ve.asset.asset_id][0],
                             mime_type=loaded[ve.asset.asset_id][1])
                  for alias, ve in bundle.alias_to_visual.items()]
        t_gen = time.perf_counter()
        try:
            raw = self.llm.generate_answer(bundle.system_instruction, bundle.user_prompt, images=images)
        except ProviderError:
            # Provider outages remain errors; malformed structured output is also an explicit provider error.
            raise
        answer, abstained, reason, citations, visual, related = self.prompt_builder.verify_and_resolve(
            raw, bundle, contextual_q)
        repairs = 0
        if abstained and raw.claims and not raw.abstained and raw.answer_mode == "grounded_answer":
            # One bounded regeneration on grounding failures, never acceptance of invalid evidence.
            repair_instruction = (bundle.system_instruction +
                "\nLa respuesta anterior no superó la verificación: citas no literales o números, "
                "unidades, comparadores o identificadores sin soporte. Relee los fragmentos. "
                "Usa citas breves copiadas exactamente, sin puntos suspensivos. No mezcles filas. "
                "Si puedes responder una parte, declárala; no agregues datos ni definiciones ausentes.\n" +
                "\n".join(self.prompt_builder.repair_issues(raw, bundle)))
            raw = self.llm.generate_answer(repair_instruction, bundle.user_prompt, images=images)
            bundle.validation_metadata.clear()
            answer, abstained, reason, citations, visual, related = self.prompt_builder.verify_and_resolve(
                raw, bundle, contextual_q)
            repairs = 1
        bundle.validation_metadata["grounding_repairs"] = repairs
        generation_ms = int((time.perf_counter() - t_gen) * 1000)
        metrics = self.prompt_builder.compute_metrics(raw, bundle, citations, visual, abstained)
        elapsed = int((time.perf_counter() - t_start) * 1000)
        return RAGAnswer(answer=answer, abstained=abstained, abstention_reason=reason,
            evidence_coverage="none" if abstained else "partial", citations=citations,
            primary_visual=visual, related_visuals=related, retrieved=len(scored),
            model=self.llm.model_name, latency_ms=elapsed,
            timings_ms={"retrieval_ms": retrieval_ms, "generation_ms": generation_ms, "total_ms": elapsed},
            metrics=metrics, answer_mode=raw.answer_mode, message_id=uuid.uuid4().hex,
            metadata={**bundle.validation_metadata, "retrieval_question": contextual_q,
                      "retrieved_sources": source_preview,
                      "supports_images": supports_images,
                      "available_related_visual_ids": list(bundle.available_related_visual_ids)})
