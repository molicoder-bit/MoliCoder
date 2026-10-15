import os
from pathlib import Path

from local_model_chat.local_actions import requested_folder
from local_model_chat.local_actions import folder_intent


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


def test_reported_word_order_and_quoted_paths(tmp_path):
    assert requested_folder(f'Let\'s create a new folder called "Messi" in {tmp_path}') == tmp_path/'Messi'
    parent = tmp_path/'parent with spaces'; parent.mkdir()
    assert requested_folder(f'Please create a folder at "{parent}/new folder"') == parent/'new folder'
    assert requested_folder(f'create a folder named "new folder" in "{parent}"') == parent/'new folder'


def test_recognized_invalid_requests_need_clarification_instead_of_inference(tmp_path):
    for request in ['create a folder called "Messi"', f'create a folder at "{tmp_path}/missing/new"',
                    f'create a folder named ".." in {tmp_path}']:
        intent = folder_intent(request)
        assert intent is not None and intent.error and intent.target is None
    assert folder_intent('Explain how folder creation works') is None
