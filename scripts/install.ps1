$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "dist\MoliCoder"))
$installRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\MoliCoder"))
$expectedParent = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$expectedPrefix = $expectedParent.TrimEnd('\') + '\'
$installToken = [guid]::NewGuid().ToString("N")
$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $expectedParent "MoliCoder.installing-$installToken"))
$backupStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = [System.IO.Path]::GetFullPath((Join-Path $expectedParent "MoliCoder.previous-$backupStamp-$installToken"))

if (-not (Test-Path -LiteralPath (Join-Path $source "MoliCoder.exe"))) {
    throw "Build not found. Run scripts\build.ps1 first."
}
foreach ($target in @($installRoot, $stagingRoot, $backupRoot)) {
    if (-not $target.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unexpected install path: $target"
    }
}

$installedExecutable = Join-Path $installRoot "MoliCoder.exe"
$runningInstall = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "MoliCoder.exe" -and
    $_.ExecutablePath -eq $installedExecutable
}
if ($runningInstall) {
    throw "Close MoliCoder before installing the update. Running process: $($runningInstall.ProcessId -join ', ')"
}

New-Item -ItemType Directory -Path $stagingRoot | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $stagingRoot -Recurse -Force

$backupCreated = $false
try {
    if (Test-Path -LiteralPath $installRoot) {
        Move-Item -LiteralPath $installRoot -Destination $backupRoot
        $backupCreated = $true
    }
    Move-Item -LiteralPath $stagingRoot -Destination $installRoot
} catch {
    if ($backupCreated -and -not (Test-Path -LiteralPath $installRoot)) {
        Move-Item -LiteralPath $backupRoot -Destination $installRoot -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    throw
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "MoliCoder.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installRoot "MoliCoder.exe"
$shortcut.WorkingDirectory = $installRoot
$shortcut.Description = "Local-first coding orchestration with Ollama"
$shortcut.Save()

Write-Host "Installed: $installRoot"
if ($backupCreated) {
    Write-Host "Previous install preserved: $backupRoot"
}
Write-Host "Shortcut: $shortcutPath"
