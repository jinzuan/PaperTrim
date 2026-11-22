# Build a standalone Windows folder for PaperTrim.
# Requires Python 3.10+ on PATH and network access for PyInstaller on first run.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install --upgrade pyinstaller
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --console `
  --name PaperTrim `
  --paths $Root `
  --add-data "$Root\web;web" `
  --add-data "$Root\LICENSE;." `
  --add-data "$Root\THIRD_PARTY_NOTICES.md;." `
  --collect-submodules uvicorn `
  --collect-all cv2 `
  "$Root\run.py"

Write-Host ""
Write-Host "Build finished: $Root\dist\PaperTrim\PaperTrim.exe"
Write-Host "Copy the whole dist\PaperTrim folder; workspace\ will be created next to the exe."
