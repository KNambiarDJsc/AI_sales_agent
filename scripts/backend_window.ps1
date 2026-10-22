# Keeps the API server running in this window: starts Postgres if needed, runs the
# server, and restarts it within seconds if it ever stops. Output is shown here and
# also written to %LOCALAPPDATA%\voiceagent-backend.log.
# Usage (from the repo root):  scripts\start_backend.cmd   (opens this in its own window)
# Close this window (or Ctrl+C) to stop the backend.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$host.UI.RawUI.WindowTitle = "AI Sales Agent - Backend (keep open)"
$log = Join-Path $env:LOCALAPPDATA "voiceagent-backend.log"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

# The tunnel watchdog (scripts\tunnel_watchdog.ps1), once: replaces an expired tunnel.
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*tunnel_watchdog.ps1*' }
if (-not $running) {
    Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSScriptRoot\tunnel_watchdog.ps1`"" -WindowStyle Hidden
    Write-Host "Tunnel watchdog started (log: %LOCALAPPDATA%\voiceagent-watchdog.log)" -ForegroundColor Green
}

while ($true) {
    $pg = Join-Path $env:LOCALAPPDATA "voiceagent-pg"
    & "$pg\pgsql\bin\pg_isready.exe" -h localhost -p 55434 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Starting Postgres..."
        Start-Process -FilePath "$pg\pgsql\bin\pg_ctl.exe" -ArgumentList "-D `"$pg\data`" -o `"-p 55434`" -l `"$pg\pg.log`" start" -WindowStyle Hidden
        Start-Sleep -Seconds 5
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "=== $stamp backend starting on http://127.0.0.1:8000 ===" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
    # No --reload: a reload in the middle of a phone call would drop the call.
    & .\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 2>&1 |
        ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "=== $stamp backend stopped - restarting in 3 s (close this window to stop it for good) ===" |
        Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
