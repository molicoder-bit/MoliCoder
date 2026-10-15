import gzip
import hashlib
import io
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.install import install_managed_archive
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _archive(name="ollama"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as contents:
        data = b"universal Mac Ollama fixture"
        info = tarfile.TarInfo(name)
        info.size = len(data)
        contents.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue())


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
def test_pinned_macos_universal_tgz_installs_for_both_architectures(tmp_path, architecture) -> None:
    store = ChatStore(tmp_path / "mac.sqlite3")
    asset = _archive()
    original = next(item for item in load_manifests()
                    if item.tool_id == "ollama" and item.system == "Darwin"
                    and item.architecture == architecture)
    manifest = replace(original, size_bytes=len(asset), sha256=hashlib.sha256(asset).hexdigest())
    environment = EnvironmentReport(
        1, "Darwin", architecture, 4, 16 * 1024**3, 20 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("ollama", None, None, "missing"),), (),
    )
    plan = plan_setup(store, environment, manifest)
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    def download(_url, target, _size, _timeout):
        target.write_bytes(asset)

    receipt = install_managed_archive(
        store, plan_setup(store, environment, manifest), manifest,
        tmp_path / "managed", downloader=download,
    )
    assert receipt.executable.endswith("ollama")
    assert (tmp_path / "managed" / f"ollama-{manifest.version}" / "ollama").read_bytes() == b"universal Mac Ollama fixture"


def test_macos_tgz_rejects_traversal(tmp_path) -> None:
    store = ChatStore(tmp_path / "mac.sqlite3")
    asset = _archive("../outside")
    original = next(item for item in load_manifests()
                    if item.tool_id == "ollama" and item.system == "Darwin")
    manifest = replace(original, size_bytes=len(asset), sha256=hashlib.sha256(asset).hexdigest())
    environment = EnvironmentReport(
        1, "Darwin", original.architecture, 4, 16 * 1024**3, 20 * 1024**3,
        "unknown", "fixture", (DiscoveredTool("ollama", None, None, "missing"),), (),
    )
    plan = plan_setup(store, environment, manifest)
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    def download(_url, target, _size, _timeout):
        target.write_bytes(asset)

    with pytest.raises(ValueError, match="unsafe path"):
        install_managed_archive(store, plan_setup(store, environment, manifest), manifest,
                                tmp_path / "managed", downloader=download)
    assert not (tmp_path / "outside").exists()
