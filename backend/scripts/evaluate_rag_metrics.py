"""Evaluate an indexed corpus against a supplied, annotated JSON dataset.

No implicit seed or API calls beyond the configured query provider. Example:
  python backend/scripts/evaluate_rag_metrics.py --dataset cases.json --output results.json
Dataset: {"split":"held_out", "cases":[{"id":"N1", "question":"...",
"expect_abstention":true, "document_ids":["doc_..."], "gold_sources":[]}]}
A gold source is {"document_id":"...", "page":2, "modality":"text|table|visual"}.
Source location agreement does NOT assess whether the cited source entails a claim.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class GoldSourceSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    document_id: str
    page: int = Field(ge=1)
    modality: Literal["text", "table", "visual"] | None = None
    group: str | None = None


class HistoryTurnSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1,max_length=2500)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str
    question: str = Field(min_length=1,max_length=2500)
    document_family: str
    split: Literal["fixed_regression","development","held_out"] = "fixed_regression"
    source_kind: Literal["synthetic","independent"]
    modality: Literal["text","table","visual","mixed"]
    expect_abstention: bool
    document_ids: list[str] | None = None
    document_key: str | None = None
    gold_sources: list[GoldSourceSchema] = Field(default_factory=list)
    expected_answer_terms: list[str] = Field(default_factory=list)
    history: list[HistoryTurnSchema] = Field(default_factory=list)
    requires_images: bool = False
    annotation_note: str


def load_cases(path: Path) -> list[dict]:
    raw=path.read_text(encoding="utf-8-sig")
    values = ([json.loads(line) for line in raw.splitlines() if line.strip()]
              if path.suffix.lower()==".jsonl" else json.loads(raw)["cases"])
    cases=[EvaluationCase.model_validate(value).model_dump() for value in values]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Case IDs must be unique")
    family_splits={}
    for case in cases:
        previous=family_splits.setdefault(case["document_family"],case["split"])
        if previous != case["split"]:
            raise ValueError("All variants of a document family must stay in the same split")
        if case["modality"]=="visual" and not case["annotation_note"].strip():
            raise ValueError("Visual cases require an annotation note describing image review")
    return cases


def write_template(path: Path) -> None:
    value=dict(id="replace-with-case-id",question="Replace with a question checked against the manual",
        document_family="independent-family-name",split="held_out",source_kind="independent",
        modality="text",expect_abstention=False,document_ids=["replace-with-indexed-document-id"],
        gold_sources=[dict(document_id="replace-with-indexed-document-id",page=1,modality="text")],
        expected_answer_terms=[],history=[],
        annotation_note="Record source revision and human-checked facets; keep paraphrases in this same split.")
    path.write_text(json.dumps(value,ensure_ascii=False)+"\n",encoding="utf-8")


def evaluate_cases(container, cases: list[dict]) -> dict:
    from app.domain.entities import ConversationTurn
    records = []
    negatives = positives = false_accepts = false_abstentions = 0
    source_hits = source_cases = 0
    citation_matches = citation_count = gold_hits = gold_count = 0
    positive_passes = positive_checked = 0
    unsupported = 0
    for case in cases:
        if not isinstance(case.get("expect_abstention"), bool):
            raise ValueError("Each case requires an explicit boolean expect_abstention label")
        query = container.query_for(case.get("document_ids")) if hasattr(container,"query_for") else container.query_use_case
        if case.get("requires_images") and not getattr(getattr(query,"llm",None),"supports_images",False):
            unsupported += 1
            records.append(dict(id=case["id"], modality=case.get("modality"),
                status="unsupported_capability", reason="Provider cannot inspect image pixels", passed=None))
            continue
        result = query.execute(
            question=case["question"], document_ids=case.get("document_ids"),
            history=[ConversationTurn(t["role"], t["content"]) for t in case.get("history",[])])
        negative = case["expect_abstention"]
        negatives += int(negative)
        positives += int(not negative)
        false_accepts += int(negative and not result.abstained)
        false_abstentions += int(not negative and result.abstained)
        gold = case.get("gold_sources", [])
        source_agreement = None
        if gold and not negative:
            source_cases += 1
            source_agreement = any(
                c.document_id == g["document_id"] and c.page == g["page"]
                and (g.get("modality") is None or c.modality == g["modality"])
                for c in result.citations for g in gold)
            source_hits += int(source_agreement)
        def matches(c,g):
            return c.document_id == g["document_id"] and c.page == g["page"] and (
                g.get("modality") is None or c.modality == g["modality"])
        terms = {term:bool(re.search(r"(?<![\w])"+re.escape(term)+r"(?![\w])",result.answer,re.I))
                 for term in case.get("expected_answer_terms",[])}
        if gold and not negative:
            citation_count += len(result.citations)
            citation_matches += sum(any(matches(c,g) for g in gold) for c in result.citations)
            groups = {}
            for g in gold:
                key = g.get("group") or (g["document_id"],g["page"],g.get("modality"))
                groups.setdefault(key,[]).append(g)
            gold_count += len(groups)
            gold_hits += sum(any(matches(c,g) for c in result.citations for g in alternatives)
                             for alternatives in groups.values())
        # This gate checks the explicit gold facets and locations, not semantic truth.
        checked = bool(gold and terms) and not negative
        case_pass = result.abstained if negative else (
            not result.abstained and all(terms.values())
            and all(any(matches(c,g) for c in result.citations for g in alternatives)
                    for alternatives in groups.values())
            and all(any(matches(c,g) for g in gold) for c in result.citations)) if checked else None
        if checked:
            positive_checked += 1
            positive_passes += int(case_pass)
        records.append(dict(
            status="evaluated", passed=case_pass,
            id=case["id"], document_family=case.get("document_family"),
            split=case.get("split"), source_kind=case.get("source_kind"), modality=case.get("modality"),
            expected_abstention=negative, actual_abstention=result.abstained,
            expected_terms_agreement=terms,
            source_location_agreement=source_agreement,
            answer=result.answer, answer_mode=result.answer_mode,
            citations=[dict(document_id=c.document_id,page=c.page,modality=c.modality)
                       for c in result.citations],
            sent_visual_ids=result.metadata.get("sent_visual_ids",[]),
            latency_ms=result.latency_ms,
            semantic_support_score=None, human_review_required=not result.abstained,
        ))
    def ratio(n, d):
        return dict(numerator=n, denominator=d, value=n/d if d else None)
    return dict(
        case_count=len(cases), evaluated_case_count=len(cases)-unsupported,
        unsupported_capability_count=unsupported,
        source_location_precision=ratio(citation_matches,citation_count),
        source_location_recall=ratio(gold_hits,gold_count),
        positive_facets_and_locations_pass_rate=ratio(positive_passes,positive_checked),
        gate_passed=all(record.get("passed") is not False for record in records),
        all_cases_evaluated=unsupported == 0,
        false_accept_rate=ratio(false_accepts,negatives),
        false_abstention_rate=ratio(false_abstentions,positives),
        source_location_agreement=ratio(source_hits,source_cases),
        semantic_citation_precision=None, faithfulness=None,
        limitations=["Source location agreement is not semantic citation precision.",
                     "No claim entailment or visual correctness judge has been run.",
                     "Report results per provider and modality; do not infer population accuracy from small sets."],
        cases=records,
    )


def _run_synthetic(cases: list[dict], *, configured_provider: bool = False) -> dict:
    from app.infrastructure.config import Settings
    from app.infrastructure.sample_manual import build_sample_manual_pdf
    from app.presentation.dependencies import AppContainer
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)
        options = dict(environment="test",data_dir=path/"data",embedding_provider="local",
            vector_backend="memory",queue_backend="background",auto_seed_sample_manual=False,
            pipeline_version="layout-v3",auth_enabled=False)
        if not configured_provider:
            options["llm_provider"] = "local"
        container=AppContainer(Settings(**options))
        try:
            from scripts.synthetic_corpus import build_synthetic_corpus
            requested = {case.get("document_key") for case in cases}
            corpus = build_synthetic_corpus(path, requested)
            if "hx900_demo" in requested:
                corpus["hx900_demo"] = build_sample_manual_pdf(path/"Manual_Turbina_HX900.pdf")
            if requested != corpus.keys() or any(case.get("source_kind") != "synthetic" for case in cases):
                raise ValueError("Synthetic mode accepts only explicitly constructed fixture keys")
            ids, hashes = {}, {}
            for key,pdf in corpus.items():
                with pdf.open("rb") as stream:
                    job,_=container.ingest_use_case.submit_stream(pdf.name,stream)
                state = container.ingest_use_case.execute_job(job.job_id)
                if state.status.value != "completed":
                    raise RuntimeError("Synthetic fixture failed indexing: "+str(state.error))
                ids[key]=state.document_id
                hashes[key]=hashlib.sha256(pdf.read_bytes()).hexdigest()
            resolved=[]
            for case in cases:
                document_id = ids[case["document_key"]]
                resolved.append({**case,"document_ids":[document_id],"gold_sources":[
                    {**gold,"document_id":document_id} for gold in case.get("gold_sources",[])]})
            report=evaluate_cases(container,resolved)
            report.update(provider=container.active_provider_name,model=container.llm_provider.model_name,
                corpus_kind="synthetic_constructed_fixtures",document_family_count=len(corpus),
                source_pdf_sha256=hashes,
                runtime_versions={name:importlib.metadata.version(name)
                                  for name in ("PyMuPDF","qdrant-client","pydantic")},
                independent_generalization_evaluated=False)
            return report
        finally:
            container.close()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--write-template",type=Path)
    parser.add_argument("--validate-only",action="store_true")
    parser.add_argument("--synthetic-demo",action="store_true")
    parser.add_argument("--synthetic-configured-provider",action="store_true",
        help="Explicitly use the configured LLM on isolated synthetic fixtures; may consume API quota")
    args=parser.parse_args()
    if args.write_template:
        write_template(args.write_template)
        return
    if args.dataset is None:
        parser.error("--dataset is required unless --write-template is used")
    if args.synthetic_configured_provider and not args.synthetic_demo:
        parser.error("--synthetic-configured-provider requires --synthetic-demo")
    cases=load_cases(args.dataset)
    if args.validate_only:
        print(json.dumps(dict(valid=True,case_count=len(cases))))
        return
    if args.synthetic_demo:
        report=_run_synthetic(cases,configured_provider=args.synthetic_configured_provider)
    else:
        from app.presentation.dependencies import get_container
        container=get_container()
        report=evaluate_cases(container,cases)
        report.update(provider=container.active_provider_name,model=container.llm_provider.model_name,
                      corpus_kind="configured_index",independent_generalization_evaluated=False)
    report.update(dataset=str(args.dataset),splits=sorted({case["split"] for case in cases}),
                  document_families=sorted({case["document_family"] for case in cases}))
    from datetime import datetime, timezone
    from measurement_provenance import measurement_provenance
    report["provenance"] = measurement_provenance()
    report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    content=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.write_text(content,encoding="utf-8")
    else:
        print(content)
    if not report["gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
