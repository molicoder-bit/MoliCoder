from pathlib import Path

from local_model_chat.kandev.discovery import discover_kandev


def test_discovers_existing_binary_and_config_without_changing_them(tmp_path) -> None:
    binary = tmp_path / "kandev.exe"
    binary.write_bytes(b"fixture")
    root = tmp_path / "existing-data"
    root.mkdir()
    config = root / "config.yaml"
    config.write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")
    before = (binary.read_bytes(), config.read_bytes(), tuple(root.iterdir()))

    result = discover_kandev(
        binary=binary, environ={"KANDEV_HOME_DIR": str(root), "KANDEV_BACKEND_PORT": "38429"},
        home=tmp_path / "unused", cwd=tmp_path / "empty",
        probe_version=lambda _path: "0.97.0",
    )

    assert result.executable == str(binary.resolve())
    assert result.version == "0.97.0"
    assert result.endpoint == "http://127.0.0.1:38429/mcp"
    assert result.data_path == str(root.resolve())
    assert result.config_path == str(config)
    assert result.compatibility == "pinned_version"
    assert (binary.read_bytes(), config.read_bytes(), tuple(root.iterdir())) == before


def test_missing_or_new_version_stays_unverified(tmp_path) -> None:
    absent = discover_kandev(which=lambda _name: None, environ={}, home=tmp_path, cwd=tmp_path)
    assert absent.executable is None and absent.version is None
    assert absent.endpoint is None and absent.compatibility == "unverified"
    assert absent.data_path == str((tmp_path / ".kandev").resolve())

    binary = tmp_path / "kandev"
    binary.touch()
    newer = discover_kandev(binary=binary, environ={}, home=tmp_path, cwd=tmp_path,
                            probe_version=lambda _path: "99.0.0")
    assert newer.compatibility == "unverified"


def test_cwd_config_precedes_home_and_invalid_port_is_not_claimed(tmp_path) -> None:
    cwd = tmp_path / "project"
    root = tmp_path / "data"
    cwd.mkdir()
    root.mkdir()
    (cwd / "config.yaml").write_text("first", encoding="utf-8")
    (root / "config.yaml").write_text("second", encoding="utf-8")

    found = discover_kandev(which=lambda _name: None, data_path=root,
                            environ={"KANDEV_PORT": "99999"}, cwd=cwd)

    assert found.config_path == str(cwd / "config.yaml")
    assert found.endpoint is None
