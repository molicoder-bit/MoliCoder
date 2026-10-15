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

$distribution = Join-Path $projectRoot "dist\MoliCoder"
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination (Join-Path $distribution "LICENSE")
Copy-Item -LiteralPath (Join-Path $projectRoot "NOTICE") -Destination (Join-Path $distribution "NOTICE")

$builtExecutable = Join-Path $projectRoot "dist\MoliCoder\MoliCoder.exe"
$healthProcess = Start-Process -FilePath $builtExecutable -ArgumentList "--package-health-check" -WindowStyle Hidden -PassThru
if (-not $healthProcess.WaitForExit(15000)) {
    Stop-Process -Id $healthProcess.Id -ErrorAction SilentlyContinue
    throw "Packaged health check timed out. The executable may be showing a startup error."
}
if ($healthProcess.ExitCode -ne 0) {
    throw "Packaged health check failed with exit code $($healthProcess.ExitCode)."
}

Write-Host "Packaged health check passed."
Write-Host "Built: $builtExecutable"
