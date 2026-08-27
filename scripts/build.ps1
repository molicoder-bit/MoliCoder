$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv (Join-Path $projectRoot ".venv")
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $projectRoot "requirements.lock")
& $python -m pip install --disable-pip-version-check --no-deps -e $projectRoot
& $python -m pytest (Join-Path $projectRoot "tests")
& $python -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "LocalModelChat.spec") --distpath (Join-Path $projectRoot "dist") --workpath (Join-Path $projectRoot "build")

Write-Host "Built: $(Join-Path $projectRoot 'dist\LocalModelChat\LocalModelChat.exe')"
