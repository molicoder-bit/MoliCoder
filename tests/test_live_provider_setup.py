"""Real native inference after the headless project permission dialog."""
import json
import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox

from local_model_chat.ollama import OllamaClient
from local_model_chat.orchestration.catalog import CapabilityCatalogService
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('MOLICODER_LIVE_PROVIDER_SETUP') != '1',
    reason='set MOLICODER_LIVE_PROVIDER_SETUP=1 for real provider inference')]


def test_real_conversation_after_project_permission(qtbot, tmp_path, monkeypatch):
    root = tmp_path / 'orchard'; root.mkdir()
    source = 'def visible_names(rows):\n    return [row["name"].strip() for row in rows if not row.get("hidden", False)]\n'
    (root / 'catalog.py').write_text(source, encoding='utf-8')
    (root / 'README.md').write_text('# Orchard\nReturns visible names with surrounding whitespace removed.\n', encoding='utf-8')
    store = ChatStore(tmp_path / 'conversation.db')
    project = store.upsert_project('Orchard', str(root), 'python')
    client = OllamaClient()
    catalog = CapabilityCatalogService(store, client)
    facts = catalog.refresh()
    assert any(r['tool_id'] == 'opencode' and r['installed'] for r in facts['runtimes'])
    # Exercise a real unavailable-local condition, preserving the ordinary
    # coordinator and native adapter. No fabricated assistant replies.
    for offering in catalog.registry.list_offerings():
        if offering.provider_id == 'ollama-local':
            catalog.registry.set_offering_state(offering.id, status='disabled', actor='test', reason='Qualify provider setup')
    w = MainWindow(store, client, start_ollama_check=False); qtbot.addWidget(w)
    w._load_project_list(select_id=project.id)
    prompts, transcript = [], []
    def select(dialog):
        assert dialog.windowTitle() == 'Another provider is needed'
        prompts.append(dialog.windowTitle())
        dialog.findChild(QCheckBox, 'allow_opencode').setChecked(True)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        return dialog.result()
    monkeypatch.setattr(QDialog, 'exec', select)
    report = Path(os.environ.get('MOLICODER_PROVIDER_SETUP_REPORT', str(tmp_path / 'conversation.json')))
    def send(text):
        w.composer.setPlainText(text); w._send_message()
        qtbot.waitUntil(lambda: w.chat_worker is None, timeout=250000)
        reply = w._current_messages()[-1]
        receipt = store.message_receipt(reply.id)
        transcript.append({'input': text, 'answer': reply.content, 'receipt': receipt})
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(transcript, indent=2), encoding='utf-8')
        assert receipt['outcome'] == 'completed', reply.content
        assert receipt['provider'] == 'opencode'
        return reply.content.lower()
    assert 'names' in send('I just opened this repo. What does it do?')
    answer = send('Where does it remove whitespace? Do not edit anything.')
    assert 'catalog.py' in answer and 'strip' in answer
    assert len(prompts) == 1
    assert store.get_project_preferences(project.id).trusted_providers == ('opencode',)
    assert (root / 'catalog.py').read_text(encoding='utf-8') == source
    assert sorted(p.name for p in root.iterdir()) == ['README.md', 'catalog.py']
