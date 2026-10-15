import os
from pathlib import Path

from local_model_chat.local_actions import requested_folder


def test_explicit_folder_request_normalizes_double_drive_colon(tmp_path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    text = f'oh that\'s fine, can you create a folder in {parent} called "ThisIsATestTosee"'
    assert requested_folder(text) == parent / "ThisIsATestTosee"


def test_folder_request_rejects_traversal_and_missing_parent(tmp_path) -> None:
    assert requested_folder(f'create a folder in {tmp_path} called ".."') is None
    assert requested_folder(f'create a folder in {tmp_path / "missing"} called "safe"') is None
    assert requested_folder("Hi") is None


def test_windows_double_colon_is_corrected_before_confirmation() -> None:
    if os.name != "nt" or not Path("C:/Dev").is_dir():
        return
    request = 'oh that\'s fine, can you create a folder in c::/Dev called "ThisIsATestTosee"'
    assert requested_folder(request) == Path("C:/Dev/ThisIsATestTosee")
