"""Evaluation denominators stay explicit; provenance metrics cannot masquerade as truth."""
from types import SimpleNamespace

from scripts.evaluate_rag_metrics import evaluate_cases
from app.domain.entities import RAGAnswer


def test_abstention_denominators_and_nonexistent_truth_score():
    responses=iter([
        RAGAnswer("No soporte",True,"missing","none",[],None,[],0,"local",1),
        RAGAnswer("extracto",False,None,"partial",[],None,[],1,"local",1),
    ])
    container=SimpleNamespace(query_use_case=SimpleNamespace(execute=lambda **kwargs:next(responses)))
    report=evaluate_cases(container,[
        {"id":"negative","question":"negative","expect_abstention":True},
        {"id":"positive","question":"positive","expect_abstention":False},
    ])
    assert report["false_accept_rate"] == {"numerator":0,"denominator":1,"value":0}
    assert report["false_abstention_rate"] == {"numerator":0,"denominator":1,"value":0}
    assert report["source_location_agreement"]["value"] is None
    assert report["faithfulness"] is None
    assert report["semantic_citation_precision"] is None



def test_dataset_variants_cannot_leak_between_development_and_heldout(tmp_path):
    import json
    import pytest
    from scripts.evaluate_rag_metrics import load_cases
    common=dict(question="Q",document_family="same-family",source_kind="independent",modality="text",
                expect_abstention=True,annotation_note="Human checked absent fact")
    path=tmp_path/"cases.jsonl"
    path.write_text(json.dumps({**common,"id":"one","split":"development"})+"\n"+
                    json.dumps({**common,"id":"two","split":"held_out"})+"\n",encoding="utf-8")
    with pytest.raises(ValueError,match="same split"):
        load_cases(path)


def test_template_and_synthetic_dataset_have_valid_explicit_scope(tmp_path):
    from pathlib import Path
    from scripts.evaluate_rag_metrics import load_cases,write_template
    template=tmp_path/"template.jsonl";write_template(template)
    assert load_cases(template)[0]["source_kind"]=="independent"
    cases=load_cases(Path(__file__).resolve().parents[2]/"evaluation"/"synthetic_regressions.jsonl")
    assert len(cases)==12
    assert {case["split"] for case in cases}=={"fixed_regression"}
    assert {case["document_family"] for case in cases}=={"synthetic-hx900-v1"}

def test_source_precision_penalizes_extra_locations_and_facets_gate_wrong_values():
    from types import SimpleNamespace
    citation=lambda page:SimpleNamespace(document_id="d",page=page,modality="text")
    answer=RAGAnswer("Pressure 999 bar",False,None,"partial",[citation(1),citation(9)],None,[],2,"mock",1)
    container=SimpleNamespace(query_use_case=SimpleNamespace(execute=lambda **kwargs:answer))
    report=evaluate_cases(container,[dict(id="one",question="Pressure?",expect_abstention=False,
        gold_sources=[dict(document_id="d",page=1)],expected_answer_terms=["14.5"])])
    assert report["source_location_precision"]["value"]==.5
    assert report["source_location_recall"]["value"]==1
    assert report["positive_facets_and_locations_pass_rate"]["value"]==0
    assert report["gate_passed"] is False
    assert report["faithfulness"] is None


def test_alternative_sources_share_one_recall_group():
    citation=SimpleNamespace(document_id="d",page=3,modality="table")
    answer=RAGAnswer("Threshold 75.0 C",False,None,"partial",[citation],None,[],1,"mock",1)
    container=SimpleNamespace(query_use_case=SimpleNamespace(execute=lambda **kwargs:answer))
    report=evaluate_cases(container,[dict(id="one",question="Threshold?",expect_abstention=False,
        gold_sources=[dict(document_id="d",page=p,group="threshold") for p in (2,3)],
        expected_answer_terms=["75.0"])])
    assert report["source_location_recall"]==dict(numerator=1,denominator=1,value=1)
    assert report["positive_facets_and_locations_pass_rate"]["value"]==1


def test_visual_positive_is_explicitly_unmeasured_with_text_only_provider():
    query=SimpleNamespace(llm=SimpleNamespace(supports_images=False),
        execute=lambda **kwargs:(_ for _ in ()).throw(AssertionError("must not run")))
    report=evaluate_cases(SimpleNamespace(query_use_case=query),[dict(id="v",question="Color?",
        expect_abstention=False,requires_images=True,modality="visual")])
    assert report["unsupported_capability_count"]==1
    assert report["evaluated_case_count"]==0
    assert report["all_cases_evaluated"] is False
    assert report["false_abstention_rate"]["denominator"]==0
