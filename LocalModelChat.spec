# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["run_app.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("src/local_model_chat/assets/molicodeLogo.png", "local_model_chat/assets"),
        ("src/local_model_chat/assets/MoliCoder.ico", "local_model_chat/assets"),
        ("src/local_model_chat/molicoder/planner/plan.schema.json", "local_model_chat/molicoder/planner"),
        ("src/local_model_chat/environment/manifests/releases.json", "local_model_chat/environment/manifests"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtQml"],
    noarchive=False,
    optimize=1,
)

# Codex and document-tool runtimes can add their own ICU builds to PATH. Qt on
# Windows uses the operating system ICU ABI, so packaging a PATH-provided ICU
# can make QtGui fail at import time with ERROR_PROC_NOT_FOUND.
excluded_host_binaries = {"icuuc.dll", "icuin.dll", "icudt.dll"} if "windows" in __import__("platform").system().lower() else set()
a.binaries = [
    binary
    for binary in a.binaries
    if binary[0].replace("\\", "/").rsplit("/", 1)[-1].lower() not in excluded_host_binaries
]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MoliCoder",
    icon="src/local_model_chat/assets/MoliCoder.ico" if __import__("sys").platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="MoliCoder",
)
