"""Real setup-button verification and conversation, with Qt offscreen."""
import json
import os
from pathlib import Path

import pytest

from local_model_chat.ollama import OllamaClient
from local_model_chat.orchestration.catalog import CapabilityCatalogService
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.provider_connection import show_provider_connection
from local_model_chat.ui.provider_setup import provider_status

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('MOLICODER_LIVE_PROVIDER_CONNECTION') != '1',
    reason='set MOLICODER_LIVE_PROVIDER_CONNECTION=1 for real setup and conversation')]


def test_real_setup_button_then_project_conversation(qtbot, tmp_path):
    root = tmp_path / 'seaside'; root.mkdir()
    code = 'def titles(rows):\n    return [row["title"].strip() for row in rows if not row.get("archived", False)]\n'
    (root / 'notes.py').write_text(code, encoding='utf-8')
    (root / 'README.md').write_text('# Seaside\nReturns trimmed titles for non-archived notes.\n', encoding='utf-8')
    store = ChatStore(tmp_path / 'profile.db'); project = store.upsert_project('Seaside', str(root), 'python')
    client = OllamaClient()
    CapabilityCatalogService(store, client).refresh()
    w = MainWindow(store, client, start_ollama_check=False); qtbot.addWidget(w)
    w._load_project_list(select_id=project.id)
    dialog = show_provider_connection(w, 'opencode')
    qtbot.waitUntil(lambda: dialog.worker is None, timeout=130000)
    assert dialog.last_result.state == 'ready', dialog.last_result
    assert provider_status(w, 'opencode')[0] == 'Ready'
    assert store.get_project_preferences(project.id).trusted_providers == ()
    dialog.reject()
    # Explicit consent in this isolated test profile remains a separate decision.
    store.set_project_preferences(project.id, provider='opencode', strategy='HYBRID', trusted_providers=('opencode',))
    report = Path(os.environ.get('MOLICODER_PROVIDER_CONNECTION_REPORT', str(tmp_path / 'conversation.json')))
    transcript = []
    for text in ['I just opened this project. What does it do?', 'What if a row has no archived flag?']:
        w.composer.setPlainText(text); w._send_message()
        qtbot.waitUntil(lambda: w.chat_worker is None, timeout=250000)
        answer = w._current_messages()[-1]
        receipt = store.message_receipt(answer.id)
        transcript.append({'input': text, 'answer': answer.content, 'receipt': receipt})
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(transcript, indent=2), encoding='utf-8')
        assert receipt['outcome'] == 'completed', answer.content
        assert receipt['provider'] == 'opencode'
    assert 'titles' in transcript[0]['answer'].lower()
    assert 'false' in transcript[1]['answer'].lower()
    assert (root / 'notes.py').read_text(encoding='utf-8') == code
    assert sorted(p.name for p in root.iterdir()) == ['README.md', 'notes.py']
