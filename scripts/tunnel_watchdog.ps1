# Keeps the public tunnel alive. Free Cloudflare quick tunnels expire without warning;
# Vobiz then rejects every call ("answer_url parameter is not valid"). Every 30 s this
# checks PUBLIC_BASE_URL/health; after two failures in a row it starts a new tunnel
# (scripts\start_tunnel.ps1 writes the new URL into .env) and stops the API server so
# the backend window restarts it with the new URL.
# Started by scripts\backend_window.ps1 (hidden); log: %LOCALAPPDATA%\voiceagent-watchdog.log
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$log = Join-Path $env:LOCALAPPDATA "voiceagent-watchdog.log"
function Say($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $log -Append -Encoding utf8 }

function PublicUrl {
    $line = Select-String -Path .env -Pattern '^PUBLIC_BASE_URL=' | Select-Object -First 1
    if ($line) { $line.Line.Split('=', 2)[1].Trim() } else { "" }
}

function Healthy($url) {
    if (-not $url) { return $false }
    try { return (Invoke-WebRequest -UseBasicParsing "$url/health" -TimeoutSec 10).StatusCode -eq 200 } catch { return $false }
}

function LocalUp {
    try { return (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 5).StatusCode -eq 200 } catch { return $false }
}

Say "watchdog started"
$fails = 0
while ($true) {
    Start-Sleep -Seconds 30
    if (-not (LocalUp)) { $fails = 0; continue }  # backend restarting: not the tunnel's fault
    if (Healthy (PublicUrl)) { $fails = 0; continue }
    $fails++
    Say "public URL not reachable ($fails)"
    if ($fails -lt 2) { continue }
    $fails = 0
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_tunnel.ps1 | Out-Null
        Say "new tunnel: $(PublicUrl)"
        $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) { Stop-Process -Id $conn.OwningProcess -Force; Say "API server restarted to pick up the new URL" }
    } catch {
        Say "tunnel restart failed: $_"
    }
    Start-Sleep -Seconds 30
}
