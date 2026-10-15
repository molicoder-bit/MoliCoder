import json

from local_model_chat.environment import managed_tools
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.molicoder.providers import OpenCodeProvider


def test_managed_executable_requires_matching_receipt_and_bounded_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(managed_tools.platform, "system", lambda: "Windows")
    monkeypatch.setattr(managed_tools.platform, "machine", lambda: "AMD64")
    manifest = next(item for item in load_manifests()
                    if item.tool_id == "opencode" and item.system == "Windows")
    root = tmp_path / "tools"
    destination = root / f"opencode-{manifest.version}"
    executable = destination / "opencode.exe"
    destination.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    receipt = {
        "tool_id": manifest.tool_id, "version": manifest.version,
        "sha256": manifest.sha256, "executable": str(executable),
    }
    (destination / "install-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert managed_tools.managed_executable("opencode", root=root) == executable.resolve()
    receipt["sha256"] = "0" * 64
    (destination / "install-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert managed_tools.managed_executable("opencode", root=root) is None
    receipt["sha256"] = manifest.sha256
    receipt["executable"] = str(tmp_path / "outside" / "opencode.exe")
    (destination / "install-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert managed_tools.managed_executable("opencode", root=root) is None


def test_opencode_provider_uses_receipt_backed_binary_when_path_is_missing(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "opencode.exe"
    binary.write_bytes(b"fixture")
    monkeypatch.setattr("local_model_chat.molicoder.providers.shutil.which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(managed_tools, "managed_executable", lambda tool_id: binary if tool_id == "opencode" else None)
    assert OpenCodeProvider().discover_binary() == binary
