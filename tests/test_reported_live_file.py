"""Opt-in real provider regression, shared with the packaged acceptance check."""
import os
import pytest
from local_model_chat.package_acceptance import verify_file_workflow

@pytest.mark.skipif(os.environ.get('MOLICODER_LIVE_CODEX') != '1', reason='Requires real Codex subscription calls')
def test_reported_filename_request_plans_approves_executes_and_applies(tmp_path):
    report = verify_file_workflow(tmp_path)
    assert report['status'] == 'passed'
    assert report['file_verified']
