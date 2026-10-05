<#
.SYNOPSIS
    Start the web-search proxy hidden, and tell you whether it came up.

.DESCRIPTION
    More robust than the .vbs version: checks Python is present, refuses to start
    a second copy if the port is already taken, and verifies the listen socket
    afterwards instead of failing silently.

.EXAMPLE
    pwsh -File scripts\start-proxy.ps1
    pwsh -File scripts\start-proxy.ps1 -Port 11435 -Upstream 11434
#>
[CmdletBinding()]
param(
    [int]$Port     = 11435,
    [int]$Upstream = 11434
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$proxy    = Join-Path $repoRoot 'proxy\ollama-proxy.py'
if (-not (Test-Path $proxy)) { $proxy = Join-Path $repoRoot 'ollama-proxy.py' }
if (-not (Test-Path $proxy)) { throw "ollama-proxy.py not found under $repoRoot" }

# pythonw.exe keeps the console hidden; python.exe is the fallback.
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python not found on PATH. Install Python 3.11+ first." }

if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Host "Port $Port is already in use - the proxy may already be running." -ForegroundColor Yellow
    return
}

Start-Process -FilePath $py `
              -ArgumentList @($proxy, '--port', $Port, '--upstream', $Upstream) `
              -WindowStyle Hidden

Start-Sleep -Seconds 2
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Host "proxy is up: http://127.0.0.1:$Port  ->  http://127.0.0.1:$Upstream" -ForegroundColor Green
} else {
    Write-Warning "proxy did not start listening on $Port. Check ollama-proxy.log next to the script."
}
