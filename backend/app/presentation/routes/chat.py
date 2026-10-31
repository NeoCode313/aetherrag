"""Multimodal RAG chat query endpoint."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from app.domain.entities import ConversationTurn, VisualEvidence
from app.presentation.dependencies import AppContainer, get_container
from app.presentation.schemas import (
    ChatQueryRequest,
    ChatQueryResponse,
    CitationDTO,
    RAGMetricsDTO,
    VisualEvidenceDTO,
)

router = APIRouter(prefix="/api/v1/chat", tags=["Multimodal RAG Chat"])


def _to_visual_dto(ve: VisualEvidence) -> VisualEvidenceDTO:
    a = ve.asset
    return VisualEvidenceDTO(
        asset_id=a.asset_id,
        document_id=a.document_id,
        document_name=ve.document_name,
        page=a.page,
        kind=a.kind.value,
        caption=a.caption or f"Diagrama técnico (Página {a.page})",
        figure_number=a.figure_number,
        figure_label=a.figure_label,
        inner_text=a.inner_text,
        bbox=a.bbox.to_list(),
        anchor_chunk_id=ve.anchor_chunk_id,
        anchor_bbox=ve.anchor_bbox.to_list(),
        distance=ve.distance,
        reason=ve.reason.value,
        image_url=f"/api/v1/assets/{a.document_id}/{a.file_name}",
        page_image_url=f"/api/v1/pages/{a.document_id}/{a.page}",
        width_px=a.width_px,
        height_px=a.height_px,
    )


@router.post(
    "/query",
    response_model=ChatQueryResponse,
    summary="Ejecutar consulta híbrida RAG con contexto visual espacial y guardrails anti-alucinación",
)
def query_rag(
    payload: ChatQueryRequest,
    container: AppContainer = Depends(get_container),
) -> ChatQueryResponse:
    history = [ConversationTurn(role=t.role, content=t.content) for t in payload.history]
    query = container.query_for(payload.document_ids)
    result = query.execute(
        question=payload.question,
        history=history,
        document_ids=payload.document_ids,
        top_k=payload.top_k,
    )

    result.metadata.update(generation_id=query.generation_id, embedding_profile_id=query.profile_id)
    citations_dto = [
        CitationDTO(
            ref=c.ref,
            modality=c.modality,
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            page=c.page,
            section=c.section,
            kind=c.kind.value,
            snippet=c.snippet,
            bbox=c.bbox.to_list(),
            score=c.score,
            dense_score=c.dense_score,
            sparse_score=c.sparse_score,
            page_image_url=f"/api/v1/pages/{c.storage_id or c.document_id}/{c.page}",
        )
        for c in result.citations
    ]

    primary_dto = _to_visual_dto(result.primary_visual) if result.primary_visual else None
    related_dtos = [_to_visual_dto(v) for v in result.related_visuals]
    m = result.metrics

    return ChatQueryResponse(
        answer=result.answer,
        message_id=result.message_id, answer_mode=result.answer_mode, metadata=result.metadata,
        abstained=result.abstained,
        abstention_reason=result.abstention_reason,
        evidence_coverage=result.evidence_coverage,
        citations=citations_dto,
        primary_visual=primary_dto,
        related_visuals=related_dtos,
        retrieved=result.retrieved,
        model=result.model,
        latency_ms=result.latency_ms,
        timings_ms=result.timings_ms,
        metrics=RAGMetricsDTO(**asdict(m)),
    )
