"""Build and smoke-check a macOS or Linux distribution from a prepared venv."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if platform.system() not in {"Darwin", "Linux"}:
        raise SystemExit("Use scripts/build.ps1 on Windows")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(root / "LocalModelChat.spec")],
        cwd=root, check=True, timeout=900,
    )
    distribution = root / "dist" / "MoliCoder"
    executable = distribution / "MoliCoder"
    if not executable.is_file():
        raise FileNotFoundError("PyInstaller did not produce the MoliCoder executable")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(root / name, distribution / name)
    subprocess.run([str(executable), "--package-smoke-check"], cwd=root,
                   check=True, timeout=15)
    if not (distribution / "_internal" / "local_model_chat" / "environment"
            / "manifests" / "releases.json").is_file():
        raise FileNotFoundError("packaged doctor manifest is missing")
    print(f"Packaged UI and doctor smoke passed: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
