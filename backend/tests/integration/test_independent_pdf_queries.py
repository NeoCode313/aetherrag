"""Non-demo PDFs: positive queries, literal text, document scope and honest abstention."""
from pathlib import Path

import pymupdf
import pytest

from app.domain.entities import ConversationTurn
from app.infrastructure.config import Settings
from app.presentation.dependencies import AppContainer
from app.application.use_cases.query_rag import contextualize_question


def pdf(path: Path, title: str, text: str):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((55, 70), title, fontsize=16)
        page.insert_textbox(pymupdf.Rect(55, 110, 540, 700), text, fontsize=12)
        document.save(path)
    return path


def test_independent_academic_and_maintenance_documents(tmp_path):
    container = AppContainer(Settings(_env_file=None, environment="test", data_dir=tmp_path/"data",
        llm_provider="local", embedding_provider="local", vector_backend="memory", database_url="",
        queue_backend="background", auth_enabled=False, auto_seed_sample_manual=False,
        gemini_api_key="", openai_api_key="", groq_api_key="", anthropic_api_key=""))
    try:
        academic = pdf(tmp_path/"academic-fixture.pdf", "Informe de probabilidad",
            "La distribución beta modela variables continuas que toman valores entre cero y uno. "
            "Se utiliza para describir proporciones. La distribución gamma describe tiempos de espera positivos.")
        maintenance = pdf(tmp_path/"maintenance-fixture.pdf", "Procedimiento de mantenimiento",
            "El regulador ZX-81 mantiene una presión nominal de 12 bar. "
            "La inspección del regulador ZX-81 debe realizarse cada 60 días.")
        jobs = []
        for path in [academic, maintenance]:
            job, _ = container.ingest_use_case.submit(path.name, path.read_bytes())
            assert container.ingest_use_case.execute_job(job.job_id).status.value == "completed"
            jobs.append(job)
        for question, index, required in [
            ("¿Para qué variables se utiliza la distribución beta?", 0, "beta"),
            ("La distribución gamma describe tiempos de espera positivos", 0, "gamma"),
            ("¿Cuál es la presión nominal del regulador ZX-81?", 1, "12"),
            ("¿Cada cuánto se inspecciona ZX-81?", 1, "60"),
        ]:
            answer = container.query_for([jobs[index].document_id]).execute(question,
                document_ids=[jobs[index].document_id])
            assert not answer.abstained, (question, answer.abstention_reason)
            assert required in answer.answer.lower()
            assert {c.document_id for c in answer.citations} == {jobs[index].document_id}
        unrelated = container.query_for([jobs[0].document_id]).execute(
            "puedes explicamr que es la Holgura diametral de zapata Babbitt y cual es su rango",
            document_ids=[jobs[0].document_id])
        assert unrelated.abstained
        assert unrelated.metadata["retrieved_sources"]
        assert all(s["document"] == academic.name for s in unrelated.metadata["retrieved_sources"])
    finally:
        container.close()


@pytest.mark.parametrize("question", [
    "puedes explicamr que es la Holgura diametral de zapata Babbitt y cual es su rango",
    "Explica la distribución beta y su aplicación",
    "Qué es el regulador ZX-81 y su presión nominal",
])
def test_complete_question_does_not_inherit_an_unrelated_topic(question):
    history = [ConversationTurn("user", "¿Qué significa la distribución gamma?")]
    assert contextualize_question(question, history) == question
    assert "distribución gamma" in contextualize_question("¿Y su aplicación?", history)
