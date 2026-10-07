# Starts Postgres (if needed) and the API server with auto-reload, in this window.
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1
# Browser demo: http://127.0.0.1:8000/demo
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$pg = Join-Path $env:LOCALAPPDATA "voiceagent-pg"
& "$pg\pgsql\bin\pg_isready.exe" -h localhost -p 55434 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Postgres..."
    Start-Process -FilePath "$pg\pgsql\bin\pg_ctl.exe" -ArgumentList "-D `"$pg\data`" -o `"-p 55434`" -l `"$pg\pg.log`" start" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

$env:PYTHONIOENCODING = "utf-8"
Write-Host "API server on http://127.0.0.1:8000  (demo: http://127.0.0.1:8000/demo)  - Ctrl+C to stop"
& .\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 --reload `
    --reload-dir apps --reload-dir config --reload-dir database --reload-dir llm --reload-dir orchestrator `
    --reload-dir qualification --reload-dir services --reload-dir speech --reload-dir telephony `
    --reload-dir tools --reload-dir voice --reload-dir observability
