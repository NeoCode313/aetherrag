"""Regressions reproducing the two demo failures without external API calls."""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.application.services.prompt_builder import DefensivePromptBuilder
from app.application.services.evidence_consistency import consistent
from app.domain.entities import (Chunk, ChunkKind, BoundingBox, ScoredChunk, GeneratedAnswer,
    GeneratedClaim, EvidenceReference, ImageInput, VisualAsset, VisualKind, LinkedVisual, LinkReason)
from app.domain.exceptions import ProviderContractError, RateLimitError
from app.infrastructure.llm.schemas import parse_structured_answer, response_schema
from app.infrastructure.llm.openai_adapter import OpenAILLMProvider
from app.infrastructure.llm.ollama_adapter import _translate_http_error
from app.infrastructure.resilience import CircuitBreaker, ResilientExecutor


@pytest.mark.parametrize("unit", ["m³/s", "m3/s", "kg/m³", "mm²/s"])
def test_literal_specification_table_with_compound_units_remains_valid(unit):
    from app.infrastructure.llm.local_fallback import GroundedExtractiveLLMProvider

    source = ("Tabla 1: Especificaciones de la turbina HX-900.\n"
              "| Parámetro | Valor |\n| --- | --- |\n"
              "| Potencia nominal | 145.0 MW |\n"
              "| Salto neto | 182.0 m |\n"
              f"| Caudal nominal | 88.5 {unit} |")
    chunk = Chunk("table", "doc", "manual.pdf", 1, ChunkKind.TABLE, source,
                  BoundingBox(.1, .1, .9, .5))
    builder = DefensivePromptBuilder()
    question = "¿Cuál es la potencia nominal, el salto neto y el caudal de la turbina en la Tabla 1?"
    context = builder.build(question, [ScoredChunk(chunk, .02, dense_score=.8)])
    raw = GroundedExtractiveLLMProvider().generate_answer(context.system_instruction, context.user_prompt)
    answer, abstained, _, citations, _, _ = builder.verify_and_resolve(raw, context, question)
    assert not abstained
    assert all(value in answer for value in ("145.0 MW", "182.0 m", f"88.5 {unit}"))
    assert [citation.chunk_id for citation in citations] == ["table"]


def test_compound_unit_fragments_are_not_equipment_identifiers():
    from app.application.services.hybrid_ranker import extract_technical_codes

    assert extract_technical_codes(
        "HX-900: caudal 88.5 m³/s, viscosidad 32 mm²/s, densidad 900 kg/m3; ST-104."
    ) == ["hx-900", "st-104"]


def test_equipment_identifier_prefix_still_cannot_borrow_another_codes_value():
    source = "La presión de AC-301/2 es 115 bar."
    chunk = Chunk("c", "d", "manual.pdf", 1, ChunkKind.TEXT, source, BoundingBox(.1,.1,.9,.4))
    builder = DefensivePromptBuilder()
    context = builder.build("presión AC-301", [ScoredChunk(chunk, .02, dense_score=.8)])
    claim = GeneratedClaim("La presión de AC-301 es 115 bar.", [EvidenceReference("E1", quote=source)])
    assert not builder._check_claim(claim, context)[0]


def bundle():
    source = "E-409: Presión del acumulador AC-301 inferior a 115 bar. Verificar precarga N2 de 140 bar."
    visual = VisualAsset("fig3", "run", 3, BoundingBox(.1,.4,.9,.8), VisualKind.VECTOR,
                         "fig3.png", caption="Figura 3: Circuito hidráulico", figure_number=3)
    chunk = Chunk("c1", "doc", "manual.pdf", 3, ChunkKind.TEXT, source, BoundingBox(.1,.1,.9,.3),
        visuals=[LinkedVisual(visual, .1, LinkReason.EXPLICIT_REFERENCE)])
    builder = DefensivePromptBuilder()
    return builder, builder.build("¿Qué significa E-409?", [ScoredChunk(chunk, .02, dense_score=.8)]), source


def test_bad_visual_flag_or_claim_does_not_discard_valid_textual_answer():
    builder, context, source = bundle()
    raw = GeneratedAnswer("", False, cited_ids=["E1"], used_visual_ids=["F1"], evidence_coverage="partial",
        claims=[GeneratedClaim("E-409 indica presión del acumulador AC-301 inferior a 115 bar.",
                               [EvidenceReference("E1", quote=source)]),
                GeneratedClaim("La válvula es roja.", [EvidenceReference("F1")])])
    answer, abstained, _, citations, related, _ = builder.verify_and_resolve(raw, context, "E-409")
    assert not abstained
    assert "115 bar" in answer and "roja" not in answer
    assert citations and related.asset.asset_id == "fig3"
    assert context.validation_metadata["used_visual_ids"] == []
    assert context.validation_metadata["rejected_claims"] == 1


def test_unknown_visual_only_claim_is_still_rejected():
    builder, context, _ = bundle()
    raw = GeneratedAnswer("", False, used_visual_ids=["F1"], evidence_coverage="partial",
        claims=[GeneratedClaim("La válvula es roja.", [EvidenceReference("F1")])])
    assert builder.verify_and_resolve(raw, context, "color")[1]


def test_short_table_quote_inherits_only_its_unique_row_label():
    source = ("Tabla 3: Acciones correctivas\n| Código | Acción |\n| --- | --- |\n"
              "| E-409 | Verificar precarga de N2 (140 bar) y fugas en servoválvula V-12B |\n"
              "| E-512 | Inspeccionar el cojinete a 85 °C |")
    chunk = Chunk("c", "d", "manual.pdf", 3, ChunkKind.TABLE, source, BoundingBox(.1,.1,.9,.4))
    builder = DefensivePromptBuilder()
    context = builder.build("acción correctiva E-409", [ScoredChunk(chunk,.02,dense_score=.8)])
    quote = "Verificar precarga de N2 (140 bar) y fugas en servoválvula V-12B"
    good = GeneratedClaim("Para E-409 se debe verificar precarga de N2 (140 bar) y fugas en servoválvula V-12B.",
                          [EvidenceReference("E1", quote=quote)])
    wrong_row = GeneratedClaim("Para E-512 se debe verificar precarga de N2 (140 bar) y fugas en servoválvula V-12B.",
                               [EvidenceReference("E1", quote=quote)])
    assert builder._check_claim(good, context)[0]
    assert not builder._check_claim(wrong_row, context)[0]


def test_caption_identifier_is_validated_from_its_own_source():
    source = "Figura 3: Circuito hidráulico - Bomba HP-300 y servomotor SM-01."
    chunk = Chunk("c", "d", "manual.pdf", 3, ChunkKind.FIGURE, source, BoundingBox(.1,.1,.9,.4))
    builder = DefensivePromptBuilder()
    context = builder.build("componentes", [ScoredChunk(chunk,.02,dense_score=.8)])
    refs = [EvidenceReference("E1", quote="Bomba HP-300 y servomotor SM-01.")]
    assert builder._check_claim(GeneratedClaim("La Figura 3 contiene Bomba HP-300 y servomotor SM-01.", refs), context)[0]
    assert not builder._check_claim(GeneratedClaim("La Figura 4 contiene Bomba HP-300 y servomotor SM-01.", refs), context)[0]
    assert not builder._check_claim(GeneratedClaim("La Figura 3 contiene 3 válvulas.", refs), context)[0]


def test_additional_table_reference_does_not_invalidate_a_complete_prose_source():
    table = "Tabla 3: Fallas\n| Código | Condición |\n| --- | --- |\n| E-409 | AC-301 < 115 bar (PT-301) |"
    prose = "E-409 se registra cuando la presión de AC-301, medida por PT-301, cae por debajo de 115 bar y activa ESD-86."
    chunks = [Chunk("c1", "d", "manual.pdf", 3, ChunkKind.TABLE, table, BoundingBox(.1,.1,.9,.3)),
              Chunk("c2", "d", "manual.pdf", 3, ChunkKind.TEXT, prose, BoundingBox(.1,.4,.9,.6))]
    builder = DefensivePromptBuilder()
    context = builder.build("E-409", [ScoredChunk(c,.02,dense_score=.8) for c in chunks])
    claim = GeneratedClaim(prose, [EvidenceReference("E1", quote="AC-301 < 115 bar (PT-301)"),
                                   EvidenceReference("E2", quote=prose)])
    assert builder._check_claim(claim, context)[0]


def test_response_schema_only_allows_actual_evidence_and_sent_images():
    _, context, _ = bundle()
    schema = response_schema(context.user_prompt)
    assert schema["$defs"]["ProviderEvidenceSchema"]["properties"]["evidence_id"]["enum"] == ["E1"]
    schema = response_schema(context.user_prompt, [ImageInput(alias="F1", data=b"png", mime_type="image/png")])
    assert schema["$defs"]["ProviderEvidenceSchema"]["properties"]["evidence_id"]["enum"] == ["E1", "F1"]


def test_external_model_cannot_select_extract_only_validation_for_a_paraphrase():
    source = "La potencia nominal es 145.0 MW y el salto nominal es 182.0 m."
    payload = dict(answer="", abstained=False, abstention_reason="", evidence_coverage="partial",
        cited_ids=["E1"], used_visual_ids=[], answer_mode="extractive_evidence", missing_facets=[],
        claims=[dict(statement="La turbina tiene una potencia nominal de 145.0 MW y un salto nominal de 182.0 m.",
                     evidence_refs=[dict(evidence_id="E1", quote=source)])])
    with patch("httpx.Client") as http:
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(payload)}}]}
        client = http.return_value.__enter__.return_value
        client.post.return_value = response
        llm = OpenAILLMProvider("synthetic", "test", ResilientExecutor(CircuitBreaker("mode"), max_attempts=1))
        generated = llm.generate_answer("system", "prompt")
        assert generated.answer_mode == "grounded_answer"
        chunk = Chunk("c", "d", "m.pdf", 1, ChunkKind.TABLE, source, BoundingBox(.1,.1,.8,.3))
        builder = DefensivePromptBuilder()
        context = builder.build("potencia y salto", [ScoredChunk(chunk,.02,dense_score=.8)])
        assert not builder.verify_and_resolve(generated, context, "potencia y salto")[1]


def test_quote_presentation_normalization_resolves_to_original_pdf_span():
    source = "La distribución beta está deﬁnida para valores\n  entre cero y uno."
    ref = EvidenceReference("E1", quote="La distribución beta está definida para valores entre cero y uno.")
    assert DefensivePromptBuilder._text_support(ref, source) == source
    assert DefensivePromptBuilder._text_support(EvidenceReference("E1", quote="La beta admite valores negativos."), source) is None


def test_explicit_interval_formatting_is_not_mistaken_for_decimal_comma():
    assert consistent("La beta usa el intervalo (0,1).", ["La distribución usa el intervalo (0, 1)."])
    assert consistent("La holgura es 0,1 mm.", ["La holgura es 0.1 mm."])
    assert not consistent("La holgura es 1 mm.", ["La holgura es 0,1 mm."])


def test_wire_contract_derives_flags_and_rejects_malformed_references():
    raw = parse_structured_answer(json.dumps({"claims": [{"statement": "Salida A.",
        "evidence_refs": [{"evidence_id": "F1", "quote": None}]}], "missing_facets": []}))
    assert not raw.abstained and raw.used_visual_ids == ["F1"]
    with pytest.raises(ProviderContractError):
        parse_structured_answer(json.dumps({"claims": [{"statement": "x", "evidence_refs": [
            {"evidence_id": "F1", "quote": "invented-image-quote"}]}], "missing_facets": []}))


def test_retry_after_is_honored_with_a_bounded_wait_budget():
    sleeps = []
    executor = ResilientExecutor(CircuitBreaker("quota"), max_attempts=3, sleeper=sleeps.append)
    failure = RateLimitError("quota", retry_after=9)
    task = MagicMock(side_effect=[failure, "ok"])
    assert executor.call(task) == "ok"
    assert sleeps == [9.1]
    task = MagicMock(side_effect=RateLimitError("daily quota", retry_after=3600))
    with pytest.raises(RateLimitError):
        executor.call(task)
    assert task.call_count == 1


def test_http_429_preserves_safe_retry_hint_without_echoing_vendor_body():
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.example.test/v1"),
        headers={"Retry-After": "9"}, json={"error": "private-organization-sentinel"})
    error = _translate_http_error(httpx.HTTPStatusError("untrusted", request=response.request, response=response), "Provider")
    assert isinstance(error, RateLimitError) and error.retry_after == 9
    assert "private-organization-sentinel" not in str(error)
