"""Hybrid retrieval with explicit evidence and capability-aware image attachments."""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence

from app.application.services.hybrid_ranker import HybridRanker, extract_technical_codes, normalize_technical_text
from app.application.services.prompt_builder import DefensivePromptBuilder
from app.domain.entities import ConversationTurn, ImageInput, RAGAnswer
from app.domain.exceptions import ProviderError, ValidationError
from app.domain.ports import EmbeddingProvider, FileStorage, LLMProvider, SparseEncoder, VectorStore

logger = logging.getLogger(__name__)


def contextualize_question(question: str, history: Sequence[ConversationTurn]) -> str:
    """Carry explicit user context through follow-ups; assistant text is never evidence."""
    def followup(text: str) -> bool:
        text = text.strip().lstrip("¿¡?! ")
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
        scored = self.ranker.fuse(dense, sparse, query=contextual_q, top_k=k)
        retrieval_ms = int((time.perf_counter() - t_start) * 1000)
        best_dense = max((sc.dense_score for sc in scored if sc.dense_score is not None), default=0.0)
        codes = extract_technical_codes(contextual_q)
        exact_identifier_hit = bool(codes) and any(all(re.search(
            r"(?<![\w/-])" + re.escape(code) + r"(?![\w/-])", normalize_technical_text(sc.chunk.text).lower())
            for code in codes) for sc in scored)
        # Lexical overlap is a retrieval signal, not proof that the requested property is present.
        if not scored or (best_dense < self.min_dense_score and not exact_identifier_hit):
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
                metadata={"semantic_support": "not_assessed", "retrieval_question": contextual_q})
        candidates = self.prompt_builder.collect_visuals(scored)
        loaded: dict[str, tuple[bytes, str]] = {}
        supports_images = bool(getattr(self.llm, "supports_images", False))
        if self.attach_images_to_llm and supports_images:
            for aid, visual in candidates.items():
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
                      "supports_images": supports_images,
                      "available_related_visual_ids": list(bundle.available_related_visual_ids)})
