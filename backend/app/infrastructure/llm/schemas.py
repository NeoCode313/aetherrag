"""Strict provider output contract. Invalid output is never promoted to an answer."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.entities import EvidenceReference, GeneratedAnswer, GeneratedClaim
from app.domain.exceptions import ProviderContractError


class EvidenceReferenceSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    evidence_id: str = Field(min_length=2, max_length=12, pattern=r"^[EF][1-9][0-9]*$")
    quote: str | None = None
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    visual_region: list[float] | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode="after")
    def check_span(self):
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("Both span boundaries are required together")
        if self.span_start is not None and self.span_end <= self.span_start:
            raise ValueError("Empty or inverted span")
        if self.visual_region is not None:
            x0, y0, x1, y1 = self.visual_region
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("Visual region must be normalized and ordered")
        return self


class ClaimSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    statement: str = Field(min_length=1, max_length=12000)
    evidence_refs: list[EvidenceReferenceSchema] = Field(min_length=1, max_length=12)


class AnswerSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    answer: str = Field(max_length=30000)
    abstained: bool
    abstention_reason: str = Field(max_length=1500)
    evidence_coverage: Literal["full", "partial", "none"]
    cited_ids: list[str] = Field(max_length=40)
    used_visual_ids: list[str] = Field(max_length=12)
    answer_mode: Literal["grounded_answer", "extractive_evidence"]
    claims: list[ClaimSchema] = Field(max_length=20)
    missing_facets: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def require_claims(self):
        if not self.abstained and self.evidence_coverage != "none" and not self.claims:
            raise ValueError("Non-abstained answers require claims and explicit evidence references")
        return self


def _strict_json_schema() -> dict:
    schema = AnswerSchema.model_json_schema()
    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in list(node.values()):
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


RAG_ANSWER_JSON_SCHEMA = _strict_json_schema()


def parse_structured_answer(raw_text: str, input_tokens: int | None = None,
                            output_tokens: int | None = None) -> GeneratedAnswer:
    if not isinstance(raw_text, str):
        raise ProviderContractError("invalid_json")
    cleaned = raw_text.strip()
    # A single enclosing JSON fence is harmless; surrounding prose and think blocks are rejected.
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(1)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        raise ProviderContractError("invalid_json") from None
    try:
        parsed = AnswerSchema.model_validate(data)
    except ValidationError as exc:
        # Never echo model text, extra keys, values or Pydantic's input in errors/logs.
        known = set(AnswerSchema.model_fields) | set(ClaimSchema.model_fields) | set(EvidenceReferenceSchema.model_fields)
        fields = tuple(dict.fromkeys(".".join(str(part) for part in error["loc"]
            if isinstance(part, int) or part in known) or "response"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)))[:6]
        raise ProviderContractError("schema", fields) from None
    return GeneratedAnswer(
        answer=parsed.answer, abstained=parsed.abstained,
        abstention_reason=parsed.abstention_reason, evidence_coverage=parsed.evidence_coverage,
        cited_ids=parsed.cited_ids, used_visual_ids=parsed.used_visual_ids,
        answer_mode=parsed.answer_mode, missing_facets=parsed.missing_facets,
        claims=[GeneratedClaim(statement=claim.statement, evidence_refs=[
            EvidenceReference(evidence_id=ref.evidence_id, quote=ref.quote,
                              span_start=ref.span_start, span_end=ref.span_end,
                              visual_region=tuple(ref.visual_region) if ref.visual_region else None)
            for ref in claim.evidence_refs]) for claim in parsed.claims],
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def portable_output_schema():
    """Vendor subset; the original Pydantic constraints still validate every response."""
    import copy
    schema = copy.deepcopy(RAG_ANSWER_JSON_SCHEMA)
    unsupported = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern"}
    def visit(value):
        if isinstance(value, dict):
            for key in unsupported:
                value.pop(key, None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return schema
