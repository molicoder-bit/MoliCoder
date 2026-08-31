$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "dist\MoliCoder"))
$installRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\MoliCoder"))
$expectedParent = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))

if (-not (Test-Path -LiteralPath (Join-Path $source "MoliCoder.exe"))) {
    throw "Build not found. Run scripts\build.ps1 first."
}
if (-not $installRoot.StartsWith($expectedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unexpected install path: $installRoot"
}

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $installRoot -Recurse -Force

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "MoliCoder.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installRoot "MoliCoder.exe"
$shortcut.WorkingDirectory = $installRoot
$shortcut.Description = "Local-first coding orchestration with Ollama"
$shortcut.Save()

Write-Host "Installed: $installRoot"
Write-Host "Shortcut: $shortcutPath"
