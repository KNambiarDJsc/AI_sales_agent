# Starts a free Cloudflare quick tunnel to the local API (port 8000) in the background
# and writes its public https:// address into .env as PUBLIC_BASE_URL — Vobiz needs a
# public URL to reach this laptop. The address changes every time the tunnel restarts,
# so run this BEFORE starting the server (the server reads .env at startup).
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\start_tunnel.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) { $cloudflared = (Get-Command cloudflared).Source }

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
$log = Join-Path $env:LOCALAPPDATA "voiceagent-tunnel.log"
if (Test-Path $log) { Remove-Item $log -Force }
Start-Process -FilePath $cloudflared -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8000", "--logfile", "`"$log`"" -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
if (-not $url) { throw "Tunnel did not start - see $log" }

# Rewrite PUBLIC_BASE_URL in .env, keeping the file's UTF-8 encoding intact.
$envPath = Join-Path (Get-Location) ".env"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$text = [IO.File]::ReadAllText($envPath, $utf8)
if ($text -match '(?m)^PUBLIC_BASE_URL=.*$') {
    $text = [regex]::Replace($text, '(?m)^PUBLIC_BASE_URL=.*$', "PUBLIC_BASE_URL=$url")
} else {
    $text = $text.TrimEnd() + "`r`nPUBLIC_BASE_URL=$url`r`n"
}
[IO.File]::WriteAllText($envPath, $text, $utf8)
Write-Host "Tunnel running: $url  (written to .env as PUBLIC_BASE_URL)"
Write-Host "Now start (or restart) the server: powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1"
