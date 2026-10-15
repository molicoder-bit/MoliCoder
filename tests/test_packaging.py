from pathlib import Path
import sys
import tomllib

import pytest

from local_model_chat import main as main_module


def test_frontier_plan_schema_is_declared_as_package_data() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    package_data = config["tool"]["setuptools"]["package-data"]
    assert "molicoder/planner/plan.schema.json" in package_data["local_model_chat"]
    assert "environment/manifests/releases.json" in package_data["local_model_chat"]
    assert "assets/*.ico" in package_data["local_model_chat"]


def test_windows_icon_loads_at_taskbar_and_explorer_sizes(qtbot) -> None:
    from PySide6.QtGui import QIcon

    assets = Path(__file__).resolve().parents[1] / 'src/local_model_chat/assets'
    icon = QIcon(str(assets / 'MoliCoder.ico'))
    sizes = {(size.width(), size.height()) for size in icon.availableSizes()}
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes
    for size in (16, 32, 48, 256):
        assert not icon.pixmap(size, size).isNull()


def test_package_health_check_exits_before_creating_the_ui(monkeypatch) -> None:
    class UnexpectedApplication:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("health check must not create the UI")

    monkeypatch.setattr(sys, "argv", ["MoliCoder.exe", "--package-health-check"])
    monkeypatch.setattr(main_module, "QApplication", UnexpectedApplication)

    assert main_module.main() == 0


def test_pyinstaller_spec_excludes_host_icu_dlls() -> None:
    project_root = Path(__file__).resolve().parents[1]
    spec = (project_root / "LocalModelChat.spec").read_text(encoding="utf-8").lower()

    assert "excluded_host_binaries" in spec
    for name in ("icuuc.dll", "icuin.dll", "icudt.dll"):
        assert name in spec


def test_build_script_runs_the_packaged_health_check() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert "--package-smoke-check" in script
    assert "WaitForExit" in script


def test_installer_uses_a_clean_staged_directory_and_preserves_a_backup() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "$stagingRoot" in script
    assert "$backupRoot" in script
    assert "Move-Item -LiteralPath $installRoot" in script
    assert "Get-CimInstance Win32_Process" in script
    assert "Remove-Item -LiteralPath $stagingRoot" in script
