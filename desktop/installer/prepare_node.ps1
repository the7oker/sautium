# Build-time helper: download a portable Node.js distribution into
# desktop/installer/node-portable/ so Inno Setup can bundle it.
# Run before `iscc sautium.iss`. Idempotent — skips if already populated.

$ErrorActionPreference = 'Stop'

$nodeVersion = "v20.18.0"
$archiveName = "node-$nodeVersion-win-x64"
$nodeUrl = "https://nodejs.org/dist/$nodeVersion/$archiveName.zip"

$installerDir = $PSScriptRoot
$nodeDir      = Join-Path $installerDir "node-portable"
$zipPath      = Join-Path $env:TEMP    "sautium-node-$nodeVersion.zip"
$extractDir   = Join-Path $env:TEMP    "sautium-node-extract"

if (Test-Path (Join-Path $nodeDir "node.exe")) {
    Write-Host "node-portable\ already populated; remove the directory to refresh."
    exit 0
}

if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }

Write-Host "Downloading Node.js $nodeVersion..."
Invoke-WebRequest -Uri $nodeUrl -OutFile $zipPath -UseBasicParsing

if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
New-Item -ItemType Directory -Force -Path $extractDir | Out-Null

Write-Host "Extracting..."
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

Move-Item -Path (Join-Path $extractDir $archiveName) -Destination $nodeDir

Remove-Item -Recurse -Force $extractDir
Remove-Item $zipPath

Write-Host "Done: $nodeDir"
