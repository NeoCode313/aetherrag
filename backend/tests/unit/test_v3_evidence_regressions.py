"""Regressions from the v2 audit; values and procedures are ground-truth fixtures."""
import pytest

from app.application.services.evidence_consistency import consistent
from app.application.services.prompt_builder import DefensivePromptBuilder
from app.domain.entities import (
    BoundingBox, Chunk, ChunkKind, ScoredChunk, GeneratedAnswer,
    GeneratedClaim, EvidenceReference,
)


@pytest.mark.parametrize("source,claim", [
    ("Temperatura nominal: -10 °C.", "Temperatura nominal: +10 °C."),
    ("Temperatura nominal: −10°C.", "Temperatura nominal: 10°C."),
    ("Presión inferior a 14.5 bar.", "Presión superior a 14.5 bar."),
    ("Presión menor que 14,5 bar.", "Presión debe superar 14,5 bar."),
    ("Presión <= 14.5 bar.", "Presión < 14.5 bar."),
    ("Presión al menos 14.5 bar.", "Presión como máximo 14.5 bar."),
    ("Pressure no more than 14.5bar.", "Pressure above 14.5bar."),
    ("La presión no debe superar 14.5 bar.", "La presión debe superar 14.5 bar."),
    ("Tensión menor que 12.", "Tensión mayor que 12."),
    ("Temperatura nominal: -1e1 °C.", "Temperatura nominal: 1e1 °C."),
])
def test_reversed_signed_values_and_bounds_are_rejected(source, claim):
    assert not consistent(claim, [source])


@pytest.mark.parametrize("source,claim", [
    ("Temperatura nominal: -10 °C.", "Temperatura nominal: −10°C."),
    ("La presión debe ser inferior a 14.5 bar.", "La presión debe ser inferior a 1.45 MPa."),
    ("La presión no debe superar 14.5 bar.", "La presión no debe superar 1450 kPa."),
    ("Temperatura nominal: 32 °F.", "Temperatura nominal: 0 °C."),
    ("Temperatura nominal: −40 °F.", "Temperatura nominal: -40 °C."),
    ("Pressure at least 10bar.", "Pressure at least 1MPa."),
])
def test_signed_and_dimensionally_equivalent_values_remain_supported(source, claim):
    assert consistent(claim, [source])


def test_extract_mode_cannot_label_a_new_instruction_as_a_quote():
    source = "Antes de operar, inspeccione la válvula de control."
    chunk = Chunk("c", "doc_fixture", "manual.pdf", 1, ChunkKind.TEXT,
                  source, BoundingBox(0, 0, 1, 1))
    builder = DefensivePromptBuilder()
    def resolve(statement):
        bundle = builder.build("¿Qué indica el manual?", [ScoredChunk(chunk, .1)])
        raw = GeneratedAnswer(answer="", abstained=False, evidence_coverage="partial",
            answer_mode="extractive_evidence",
            claims=[GeneratedClaim(statement, [EvidenceReference("E1", quote=source)])])
        return builder.verify_and_resolve(raw, bundle, "¿Qué indica el manual?")
    assert resolve("Desconecte todas las protecciones de la turbina antes de operar.")[1]
    valid = resolve(source)
    assert not valid[1]
    assert source in valid[0]

