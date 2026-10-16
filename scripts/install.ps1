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
$installedIcon = Join-Path $installRoot "_internal\local_model_chat\assets\MoliCoder.ico"
if (-not (Test-Path -LiteralPath $installedIcon)) {
    throw "Installed icon not found: $installedIcon"
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installRoot "MoliCoder.exe"
$shortcut.IconLocation = $installedIcon
$shortcut.WorkingDirectory = $installRoot
$shortcut.Description = "Local-first coding orchestration with Ollama"
$shortcut.Save()

# Windows retains the icon from an older taskbar pin unless that shortcut is
# updated too. Keep the existing pin and its position; only change its icon.
$pinnedDirectory = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
$updatedPins = @()
Get-ChildItem -LiteralPath $pinnedDirectory -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
    $pinnedShortcut = $shell.CreateShortcut($_.FullName)
    if ([System.String]::Equals($pinnedShortcut.TargetPath, $installedExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
        $pinnedShortcut.IconLocation = $installedIcon
        $pinnedShortcut.Save()
        $updatedPins += $_.FullName
    }
}

# Refresh just the updated executable and shortcut in Explorer's icon cache.
if (-not ("MoliCoderShellIconNotification" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MoliCoderShellIconNotification {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern void SHChangeNotify(uint eventId, uint flags,
        [MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr unused);
}
'@
}
foreach ($iconTarget in @($installedExecutable, $installedIcon, $shortcutPath) + $updatedPins) {
    [MoliCoderShellIconNotification]::SHChangeNotify(0x2000, 0x0005, $iconTarget, [IntPtr]::Zero)
}

Write-Host "Installed: $installRoot"
if ($backupCreated) {
    Write-Host "Previous install preserved: $backupRoot"
}
Write-Host "Shortcut: $shortcutPath"
