# Pack-Brain: packt das Backend und stellt es kurz per HTTP fuer den Minecraft-LXC bereit.
# Aufruf (normale PowerShell, im Ordner mc-ai):
#   powershell -ExecutionPolicy Bypass -File deploy\serve_transfer.ps1
# Optional: -Ip <PC-IP> -Port 8000
param(
    [string]$Ip = "",
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot          # ...\mc-ai
$out  = Join-Path $env:TEMP "packbrain-transfer"

# 1. Paket bauen (nur was der Server braucht; keine Logs, keine Sicherungen)
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory $out | Out-Null
Push-Location $root
try {
    tar.exe -czf (Join-Path $out "packbrain-upload.tar.gz") --exclude=__pycache__ `
        packbrain db/packbrain.sqlite db/cards.sqlite config.json requirements.txt extract/make_cards.py
    if ($LASTEXITCODE -ne 0) { throw "tar ist fehlgeschlagen" }
} finally { Pop-Location }

# install.sh mit Linux-Zeilenenden daneben legen; leere index.html verhindert eine Dateiliste
$sh = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "install.sh")) -replace "`r`n", "`n"
[IO.File]::WriteAllText((Join-Path $out "install.sh"), $sh)
[IO.File]::WriteAllText((Join-Path $out "index.html"), "")

# 2. LAN-IP des PCs bestimmen
if (-not $Ip) {
    $Ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -match '^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
           Select-Object -First 1).IPAddress
}
if (-not $Ip) {
    Write-Host "Keine private LAN-Adresse gefunden. IP angeben: -Ip <PC-IP>" -ForegroundColor Red
    Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, InterfaceAlias | Format-Table
    exit 1
}

$size = [math]::Round((Get-Item (Join-Path $out "packbrain-upload.tar.gz")).Length / 1MB)
Write-Host ""
Write-Host "Paket fertig ($size MB). Jetzt in der Proxmox-Konsole des Minecraft-LXC eingeben:" -ForegroundColor Green
Write-Host ""
Write-Host "    curl -fsSO http://${Ip}:${Port}/install.sh && bash install.sh ${Ip}:${Port}" -ForegroundColor Yellow
Write-Host ""
Write-Host "Wenn das Skript im LXC 'FERTIG' meldet: hier Strg+C druecken."
Write-Host "Achtung: Das Paket enthaelt den API-Token. Den Server nicht laenger als noetig laufen lassen."
Write-Host ""

# 3. Webserver starten (bis Strg+C), danach aufraeumen
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
try {
    & $py -m http.server $Port --bind $Ip --directory $out
} finally {
    Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
    Write-Host "Webserver beendet, Transferordner geloescht."
}
