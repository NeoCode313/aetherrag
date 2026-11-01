 $ErrorActionPreference="Stop"
& (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") -B (Join-Path $PSScriptRoot "backend\scripts\local_setup.py")
if ($LASTEXITCODE -ne 0) { throw "No se pudo abrir la configuración inicial" }
