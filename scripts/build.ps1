$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv (Join-Path $projectRoot ".venv")
if ($LASTEXITCODE -ne 0) { throw "Build command failed with exit code $LASTEXITCODE" }
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $projectRoot "requirements.lock")
if ($LASTEXITCODE -ne 0) { throw "Build command failed with exit code $LASTEXITCODE" }
& $python -m pip install --disable-pip-version-check --no-deps -e $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Build command failed with exit code $LASTEXITCODE" }
& $python -m pytest (Join-Path $projectRoot "tests")
if ($LASTEXITCODE -ne 0) { throw "Build command failed with exit code $LASTEXITCODE" }
& $python -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "LocalModelChat.spec") --distpath (Join-Path $projectRoot "dist") --workpath (Join-Path $projectRoot "build")
if ($LASTEXITCODE -ne 0) { throw "Build command failed with exit code $LASTEXITCODE" }

$distribution = Join-Path $projectRoot "dist\MoliCoder"
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination (Join-Path $distribution "LICENSE")
Copy-Item -LiteralPath (Join-Path $projectRoot "NOTICE") -Destination (Join-Path $distribution "NOTICE")

$builtExecutable = Join-Path $projectRoot "dist\MoliCoder\MoliCoder.exe"
$env:QT_QPA_PLATFORM = "offscreen"
$healthProcess = Start-Process -FilePath $builtExecutable -ArgumentList "--package-smoke-check" -WindowStyle Hidden -PassThru
if (-not $healthProcess.WaitForExit(90000)) {
    Stop-Process -Id $healthProcess.Id -ErrorAction SilentlyContinue
    throw "Packaged health check timed out. The executable may be showing a startup error."
}
if ($healthProcess.ExitCode -ne 0) {
    throw "Packaged health check failed with exit code $($healthProcess.ExitCode)."
}

Write-Host "Packaged UI and doctor smoke passed."
Write-Host "Built: $builtExecutable"

$boardProcess = Start-Process -FilePath $builtExecutable -ArgumentList "--package-board-smoke-check" -WindowStyle Hidden -PassThru
if (-not $boardProcess.WaitForExit(30000)) {
    Stop-Process -Id $boardProcess.Id -ErrorAction SilentlyContinue
    throw "Packaged board renderer timed out."
}
if ($boardProcess.ExitCode -ne 0) { throw "Packaged board renderer failed." }
Write-Host "Packaged board renderer passed."
