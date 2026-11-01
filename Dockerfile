FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/backend \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBEDDING_MODEL_DIR=/opt/aetherrag/models/e5-small \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libgomp1 tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /workspace/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /workspace/requirements.lock

COPY backend /workspace/backend
COPY frontend /workspace/frontend
COPY .env.example /workspace/.env.example

# Public immutable weights are prepared once at build time, never from private PDFs.
RUN python -B backend/scripts/download_semantic_model.py --directory /opt/aetherrag/models/e5-small \
    && chmod -R a+rX /opt/aetherrag/models

RUN groupadd --gid 10001 aetherrag \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aetherrag \
    && mkdir -p /workspace/data \
    && chown -R 10001:10001 /workspace/data

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
