<#
.SYNOPSIS
    Reinstall and launch the Parking Solver desktop app.

.DESCRIPTION
    1. Ensures a .venv exists (creates one if missing).
    2. Reinstalls the package in editable mode (picks up new deps / code).
    3. Runs an offscreen startup smoke check so a broken build never launches.
    4. Optionally runs the fast test suite (-Test).
    5. Launches the GUI.

.EXAMPLE
    .\redeploy.ps1
    .\redeploy.ps1 -Test          # run fast tests before launching
    .\redeploy.ps1 -NoLaunch      # reinstall + smoke check only
#>
param(
    [switch]$Test,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "==> No .venv found — creating one..." -ForegroundColor Yellow
    python -m venv (Join-Path $root ".venv")
}

$env:PYTHONUTF8 = "1"

Write-Host "==> Installing/updating package (editable)..." -ForegroundColor Cyan
& $py -m pip install -e $root --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed."; exit 1 }

if ($Test) {
    Write-Host "==> Running fast test suite..." -ForegroundColor Cyan
    & $py -m pytest --ignore=tests/test_polygon_validation.py -q
    if ($LASTEXITCODE -ne 0) { Write-Error "Tests failed — not launching."; exit 1 }
}

Write-Host "==> Smoke-checking app startup (offscreen)..." -ForegroundColor Cyan
$env:QT_QPA_PLATFORM = "offscreen"
& $py -c "from PySide6.QtWidgets import QApplication; from parking_solver.ui.main_window import MainWindow; app=QApplication([]); MainWindow(); print('startup OK')"
$smoke = $LASTEXITCODE
Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
if ($smoke -ne 0) { Write-Error "Startup smoke check failed — not launching."; exit 1 }

if ($NoLaunch) {
    Write-Host "==> Done (skipped launch)." -ForegroundColor Green
    exit 0
}

Write-Host "==> Launching Parking Solver..." -ForegroundColor Green
& $py -m parking_solver
