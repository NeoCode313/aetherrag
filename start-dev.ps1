param([ValidateRange(1024,65535)][int]$Port=8000)
$ErrorActionPreference="Stop"
$python=Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Create .venv and install requirements.lock first. See README." }
$env:PYTHONPATH=Join-Path $PSScriptRoot "backend"
$env:AETHERRAG_ENV_FILE=Join-Path $PSScriptRoot ".env"
& $python -B (Join-Path $PSScriptRoot "backend/scripts/download_semantic_model.py")
if ($LASTEXITCODE -ne 0) { throw "No se pudo preparar el modelo semántico" }
# Foreground process: Ctrl+C stops this exact development instance.
& $python -B -m uvicorn app.main:app --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) { throw "Development server stopped with exit code $LASTEXITCODE" }
