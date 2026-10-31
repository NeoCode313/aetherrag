"""Synthetic local concurrency smoke test; not an SLA or a real-manual benchmark.

Runs the real FastAPI app via TestClient, SQLite test catalogue, in-memory Qdrant,
local embeddings and extractive answers. No network API or production storage is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import platform
import importlib.metadata
from datetime import datetime, timezone
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import fitz
from fastapi.testclient import TestClient

from app.infrastructure.config import Settings
from app.main import create_app
from app.presentation.dependencies import AppContainer, get_container


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    values=sorted(values)
    return round(values[max(0,math.ceil(len(values)*percentile_value)-1)],3)


def latency_summary(values: list[float]) -> dict:
    return dict(count=len(values),p50_ms=percentile(values,.5),p95_ms=percentile(values,.95),
                max_ms=round(max(values),3) if values else None)


def pdf_fixture(index: int) -> bytes:
    with fitz.open() as document:
        page=document.new_page(width=595,height=842)
        page.insert_text((60,70),f"Manual sintetico LT-{index:04d}",fontsize=15)
        page.insert_text((60,120),f"La presion nominal del equipo LT-{index:04d} es {100+index} bar.",fontsize=11)
        return document.tobytes()


def run_load(count: int=100, timeout_seconds: float=300, *, chat_p95_budget_ms: float=500,
             admission_p95_budget_ms: float=1000, max_chat_degradation: float=10) -> dict:
    if not 2 <= count <= 500 or not 10 <= timeout_seconds <= 300:
        raise ValueError("count must be 2..500 and timeout must be 10..300 seconds")
    if min(chat_p95_budget_ms, admission_p95_budget_ms, max_chat_degradation) <= 0:
        raise ValueError("Performance budgets must be positive")
    fixtures=[pdf_fixture(index) for index in range(1,count+1)]
    t_start=time.perf_counter();deadline=t_start+timeout_seconds
    errors=[];admission=[];baseline=[];under_load=[];jobs=[];visibility_checks=[]
    lock=threading.Lock();finished=threading.Event()
    with tempfile.TemporaryDirectory() as folder:
        container=AppContainer(Settings(environment="test",data_dir=Path(folder)/"data",
            llm_provider="local",embedding_provider="local",vector_backend="memory",
            queue_backend="background",auto_seed_sample_manual=False,auth_enabled=False))
        app=create_app();app.dependency_overrides[get_container]=lambda:container
        with TestClient(app) as client:
            logging.getLogger("app.application.use_cases.ingest_document").setLevel(logging.WARNING)
            def upload(index):
                begin=time.perf_counter()
                response=client.post("/api/v1/documents/ingest",
                    files={"file":(f"load_{index+1:04d}.pdf",fixtures[index],"application/pdf")})
                elapsed=(time.perf_counter()-begin)*1000
                body=response.json()
                with lock:
                    admission.append(elapsed)
                    if response.status_code!=202:
                        errors.append(dict(stage="upload",index=index,status=response.status_code,body=body))
                    else:
                        jobs.append(body)
                return body if response.status_code==202 else None
            first=upload(0)
            if first is None:
                raise RuntimeError("Initial synthetic upload was rejected")
            while time.perf_counter()<deadline:
                seed_state=client.get("/api/v1/jobs/"+first["job_id"]).json()
                if seed_state["status"] in {"completed","failed"}:
                    break
                time.sleep(.05)
            if seed_state["status"]!="completed":
                raise RuntimeError("Initial fixture did not finish: "+str(seed_state))
            query={"question":"¿Cuál es la presión nominal del equipo LT-0001?",
                   "document_ids":[first["document_id"]]}
            def ask(target):
                begin=time.perf_counter()
                response=client.post("/api/v1/chat/query",json=query)
                elapsed=(time.perf_counter()-begin)*1000
                body=response.json()
                with lock:
                    target.append(elapsed)
                    if response.status_code!=200 or body.get("abstained"):
                        errors.append(dict(stage="chat",status=response.status_code,abstained=body.get("abstained")))
                    elif any(c["document_id"]!=first["document_id"] for c in body.get("citations",[])):
                        errors.append(dict(stage="chat",error="Document filter leaked another document"))
                return body
            for _ in range(8):
                ask(baseline)
            def chat_worker():
                for _ in range(200):
                    if finished.is_set() or time.perf_counter()>=deadline:
                        return
                    ask(under_load)
                    time.sleep(.01)
            def probe_visibility():
                snapshot=client.get("/api/v1/documents").json()
                pending=next((d for d in snapshot if d["status"] in {"pending","processing"}),None)
                if pending is None:
                    return
                response=client.post("/api/v1/chat/query",json={
                    "question":"¿Cuál es la presión nominal?","document_ids":[pending["document_id"]]})
                after=client.get("/api/v1/jobs/"+pending["job_id"]).json()
                invalid=(bool(response.json().get("citations")) and after["status"]!="completed")
                visibility_checks.append(dict(pending_document_id=pending["document_id"],
                    status_after_query=after["status"],staging_visible=invalid))
                if invalid:
                    errors.append(dict(stage="visibility",error="Staging evidence was visible before publication"))
            chat_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix="load-chat")
            chats=[chat_pool.submit(chat_worker) for _ in range(4)]
            try:
                with ThreadPoolExecutor(max_workers=4,thread_name_prefix="load-upload") as uploads:
                    futures=[uploads.submit(upload,index) for index in range(1,count)]
                    for done in as_completed(futures,timeout=max(1,deadline-time.perf_counter())):
                        done.result()
                        accepted=len(jobs)
                        if accepted%20==0:
                            print(json.dumps(dict(progress="accepted",count=accepted,total=count)),flush=True)
                            probe_visibility()
                complete=0
                while time.perf_counter()<deadline:
                    documents=client.get("/api/v1/documents").json()
                    complete=sum(d["status"]=="completed" for d in documents)
                    if complete==count or any(d["status"]=="failed" for d in documents):
                        break
                    time.sleep(.1)
                states=[client.get("/api/v1/jobs/"+job["job_id"]).json() for job in jobs]
                completed=sum(state["status"]=="completed" for state in states)
                active=container.catalog.active_generation()
                active_runs=container.catalog.active_run_ids(generation_id=active["generation_id"])
                document_ids=[job["document_id"] for job in jobs]
                job_ids=[job["job_id"] for job in jobs]
                invariants=dict(all_uploads_accepted=len(jobs)==count,
                    all_jobs_completed=completed==count,
                    unique_document_ids=len(set(document_ids))==count,
                    unique_job_ids=len(set(job_ids))==count,
                    active_runs_equal_document_count=len(active_runs)==count,
                    no_staging_exposure=not any(check["staging_visible"] for check in visibility_checks),
                    no_http_or_filter_errors=not errors)
                report=dict(classification="SYNTHETIC_LOCAL_SMOKE_NOT_SLA",count_requested=count,
                    upload_concurrency=4,chat_concurrency=4,ingestion_workers=2,
                    storage="SQLite test catalogue + in-memory Qdrant",provider="local-extractive-evidence",
                    corpus="Distinct one-page PDFs with one parameter each, no tables or images",
                    source_pdf_sha256=[hashlib.sha256(data).hexdigest() for data in fixtures],
                    completion_count=completed,active_run_count=len(active_runs),
                    duplicate_document_id_count=len(document_ids)-len(set(document_ids)),
                    duplicate_job_id_count=len(job_ids)-len(set(job_ids)),
                    admission=latency_summary(admission),chat_baseline=latency_summary(baseline),
                    chat_under_load=latency_summary(under_load),invariants=invariants,
                    visibility_samples=visibility_checks,errors=errors,
                    elapsed_seconds=round(time.perf_counter()-t_start,3),
                    hardware=dict(platform=platform.platform(),processor=platform.processor()),
                    limitations=["HTTP timings include in-process TestClient transport, multipart parsing and admission.",
                        "No external model, Redis broker, PostgreSQL or remote Qdrant was exercised.",
                        "These fixtures are not complex manuals; results do not establish a production SLA.",
                        "Visibility samples are race-aware smoke checks; deterministic failure-injection tests remain necessary."])
            finally:
                finished.set()
                for future in chats:
                    future.result(timeout=max(1,min(30,deadline-time.perf_counter())))
                chat_pool.shutdown(wait=True)
            # Capture all final chat observations after workers have stopped.
            report["chat_under_load"]=latency_summary(under_load)
            baseline_p95=report["chat_baseline"]["p95_ms"]
            load_p95=report["chat_under_load"]["p95_ms"]
            report["chat_p95_ratio"]=round(load_p95/baseline_p95,3) if baseline_p95 and load_p95 else None
            steady = []
            for _ in range(8):
                ask(steady)
            report["chat_post_load"] = latency_summary(steady)
            steady_p95 = report["chat_post_load"]["p95_ms"]
            report["chat_p95_ratio_vs_same_corpus"] = round(load_p95/steady_p95,3) if steady_p95 and load_p95 else None
            report["integrity_passed"] = all(report["invariants"].values()) and not errors
            report["performance_budgets"] = dict(chat_p95_ms=chat_p95_budget_ms,
                admission_p95_ms=admission_p95_budget_ms, max_chat_degradation=max_chat_degradation,
                scope="Acceptance budgets for this local synthetic harness, not a production SLA")
            report["performance_checks"] = dict(
                chat_p95_within_budget=load_p95 is not None and load_p95 <= chat_p95_budget_ms,
                admission_p95_within_budget=report["admission"]["p95_ms"] <= admission_p95_budget_ms,
                chat_degradation_within_budget=report["chat_p95_ratio_vs_same_corpus"] is not None
                    and report["chat_p95_ratio_vs_same_corpus"] <= max_chat_degradation)
            report["performance_passed"] = all(report["performance_checks"].values())
            report["passed"] = report["integrity_passed"] and report["performance_passed"]
            report["elapsed_seconds"] = round(time.perf_counter()-t_start,3)
            from measurement_provenance import measurement_provenance
            report["provenance"] = measurement_provenance()
            report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
            report["runtime_versions"] = {name:importlib.metadata.version(name) for name in ("SQLAlchemy","qdrant-client","PyMuPDF")}
            return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count",type=int,default=100)
    parser.add_argument("--timeout",type=float,default=300)
    parser.add_argument("--output",type=Path,default=Path("backend/evaluation/load_smoke_results.json"))
    parser.add_argument("--chat-p95-ms",type=float,default=500)
    parser.add_argument("--admission-p95-ms",type=float,default=1000)
    parser.add_argument("--max-chat-degradation",type=float,default=10)
    args=parser.parse_args()
    report=run_load(args.count,args.timeout,chat_p95_budget_ms=args.chat_p95_ms,
        admission_p95_budget_ms=args.admission_p95_ms,max_chat_degradation=args.max_chat_degradation)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("passed","completion_count","admission","chat_baseline","chat_under_load","elapsed_seconds")}),flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__=="__main__":
    main()
