# Build GhostBrain.exe - a self-contained installer-style exe (PyInstaller onefile)
# The exe bundles Python, FastAPI, uvicorn, Playwright AND the Chromium browser.
# Double-click GhostBrain.exe -> server starts -> open http://127.0.0.1:8000
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[0/5] Checking prerequisites..." -ForegroundColor Cyan
py -3.11 -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 without tkinter found. Install python.org 3.11 (tkinter is included by default) or fix 'py -3.11'."
}

Write-Host "[1/5] Installing dependencies (fastapi, uvicorn, playwright, pyinstaller)..." -ForegroundColor Cyan
py -3.11 -m pip install --upgrade fastapi uvicorn playwright pyinstaller

Write-Host "[2/5] Downloading Chromium browser for bundling..." -ForegroundColor Cyan
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot "browsers"
py -3.11 -m playwright install chromium
if (-not (Get-ChildItem -Directory $env:PLAYWRIGHT_BROWSERS_PATH -Filter "chromium-*" -ErrorAction SilentlyContinue)) {
    throw "Chromium download failed."
}

Write-Host "[3/5] Cleaning previous build artifacts (keeping Gemini_Profiles!)..." -ForegroundColor Cyan
Remove-Item -Recurse -Force (Join-Path $PSScriptRoot "build") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $PSScriptRoot "dist\GhostBrain.exe") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $PSScriptRoot "dist\GhostBrain.spec") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $PSScriptRoot "GhostBrain.spec") -ErrorAction SilentlyContinue

Write-Host "[4/5] Building GhostBrain.exe (onefile, this takes a few minutes)..." -ForegroundColor Cyan
py -3.11 -m PyInstaller --noconfirm --clean --onefile --windowed --name GhostBrain `
    --collect-all fastapi --collect-all uvicorn --collect-all starlette `
    --collect-all anyio --collect-all playwright --collect-all certifi `
    --hidden-import=multipart --hidden-import=pydantic `
    --add-data "ui.html;." --add-data "browsers;browsers" `
    Gemini_Ghost_Brain.py

Write-Host "[5/5] Verifying output..." -ForegroundColor Cyan
$exe = Join-Path $PSScriptRoot "dist\GhostBrain.exe"
if (-not (Test-Path $exe)) { throw "Build failed: exe not found." }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "DONE -> $exe  ($size MB)" -ForegroundColor Green
