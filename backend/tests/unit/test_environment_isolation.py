"""A real host test run must not inherit a deployment's database/provider/data."""
import os
from pathlib import Path
import subprocess
import sys


def test_host_suite_ignores_inherited_production_configuration(tmp_path):
    root = Path(__file__).resolve().parents[3]
    forbidden = tmp_path / "production-data-must-not-be-created"
    environment = dict(os.environ, ENVIRONMENT="production", DATA_DIR=str(forbidden),
        DATABASE_URL="postgresql+psycopg://unused:unused@invalid.example/never_connect",
        EMBEDDING_PROVIDER="onnx", LLM_PROVIDER="gemini", GEMINI_API_KEY="synthetic-never-use",
        SESSION_SECRET="", OPERATOR_PASSWORD_HASH="")
    result = subprocess.run([sys.executable, "-B", "-m", "pytest",
        "backend/tests/integration/test_api_and_pdf_pipeline.py::test_full_ingestion_and_multimodal_query_lifecycle",
        "-q", "-p", "no:cacheprovider"], cwd=root, env=environment,
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not forbidden.exists()
