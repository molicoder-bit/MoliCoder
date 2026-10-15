import pytest

from local_model_chat.environment.discovery import discover_environment


@pytest.mark.parametrize("system,manager", [
    ("Windows", "winget"),
    ("Darwin", "brew"),
    ("Linux", "apt"),
])
def test_read_only_inventory_across_platform_fixtures(system, manager, tmp_path) -> None:
    found = {"python": str(tmp_path / "python"), "git": str(tmp_path / "git"),
             "kandev": str(tmp_path / "kandev"), manager: str(tmp_path / manager)}
    calls = []

    def which(name):
        calls.append(name)
        return found.get(name)

    report = discover_environment(
        system=system, architecture="x86_64", cpu_count=8,
        memory_bytes=16 * 1024**3, disk_free_bytes=100 * 1024**3,
        gpu=("Test GPU, 8192 MiB", "fixture"), which=which,
        version=lambda binary: "1.0" if binary in found.values() else None,
    )

    assert report.system == system
    assert report.cpu_count == 8
    assert report.memory_bytes == 16 * 1024**3
    assert report.gpu_source == "fixture"
    assert report.package_managers == (manager,)
    assert next(tool for tool in report.tools if tool.name == "kandev").state == "installed"
    assert next(tool for tool in report.tools if tool.name == "kandev").version == "1.0"
    assert "nvidia-smi" not in calls
    assert not list(tmp_path.iterdir())


def test_missing_optional_agents_and_gpu_do_not_block_inventory(tmp_path) -> None:
    report = discover_environment(
        system="Linux", architecture="aarch64", cpu_count=4,
        memory_bytes=8 * 1024**3, disk_free_bytes=50 * 1024**3,
        gpu=("unknown", "no supported probe"), which=lambda _name: None,
    )
    assert report.gpu == "unknown"
    assert all(tool.state == "missing" for tool in report.tools)
    assert report.package_managers == ()


def test_windows_amd64_matches_pinned_x86_64_manifest() -> None:
    report = discover_environment(
        system="Windows", architecture="AMD64", cpu_count=8,
        memory_bytes=1024, disk_free_bytes=1024,
        gpu=("unknown", "fixture"), which=lambda _name: None,
    )
    assert report.architecture == "x86_64"
