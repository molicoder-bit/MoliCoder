"""Real multi-turn desktop-worker qualification, invoked from CLI with Qt offscreen.

MOLICODER_LIVE_CONVERSATION=1 enables inference. Optionally set
MOLICODER_LIVE_FALLBACK_PROVIDER=opencode for an explicitly authorized fallback.
"""
import json
import os
from pathlib import Path
import time

import pytest

from local_model_chat.domain import Message
from local_model_chat.ollama import OllamaClient
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('MOLICODER_LIVE_CONVERSATION') != '1',
    reason='set MOLICODER_LIVE_CONVERSATION=1 for real multi-turn inference')]


def test_real_developer_conversation_and_reopened_chat(qtbot, tmp_path):
    root = tmp_path/'harbor-notes'; root.mkdir()
    (root/'README.md').write_text('# Harbor Notes\nFilters active notes and trims their titles.\n')
    code = 'def active_titles(notes):\n    return [note["title"].strip() for note in notes if not note.get("archived", False)]\n'
    (root/'notes.py').write_text(code)
    store = ChatStore(tmp_path/'conversation.db')
    project = store.upsert_project('Harbor Notes',str(root),'python')
    fallback = os.environ.get('MOLICODER_LIVE_FALLBACK_PROVIDER')
    if fallback:
        providers=tuple(dict.fromkeys(name.strip() for name in fallback.split(',')))
        assert providers and set(providers)<={'opencode','kilo','codex','claude','copilot'}
        store.set_project_preferences(project.id, provider=providers[0], strategy='HYBRID', trusted_providers=providers)
    window = MainWindow(store, OllamaClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    transcript=[]
    report=Path(os.environ.get('MOLICODER_CONVERSATION_REPORT',str(tmp_path/'conversation.json')))

    def send(text, reaction=False):
        count=len(window._current_messages()); started=time.monotonic()
        window.composer.setPlainText(text);window._send_message()
        qtbot.waitUntil(lambda: window.chat_worker is None, timeout=250000)
        rows=window._current_messages()
        assert len(rows)==count+2
        answer=rows[-1].content
        receipt=store.message_receipt(rows[-1].id)
        transcript.append({'input':text,'answer':answer,'receipt':receipt,'seconds':round(time.monotonic()-started,2)})
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(transcript,indent=2),encoding='utf-8')
        assert receipt['outcome'] in ({'completed','awaiting_input'} if reaction else {'completed'}), answer
        assert answer.strip().lower() not in {'project','unknown','filename'}
        assert not answer.rstrip().endswith(':')
        return answer.split('Sources:')[0]

    window._new_chat();general=window.current_conversation_id
    assert 'General chat' in send('what project are we on')
    assert 'project' in send('uhh?',reaction=True).lower()
    send('Remember that my idea is called Docklight. Keep answers short.')
    assert 'Docklight' in send('What name did I just give it?')
    example=send('Show me a tiny Python list comprehension example without creating files.')
    assert '[' in example and 'for ' in example
    window._load_project_list(select_id=project.id);window._new_chat()
    assert 'notes' in send('I just opened this repo. What is it for?').lower()
    located=send('Where does it ignore archived entries?')
    assert 'notes.py' in located and 'archived' in located
    cleaned=send('Does it also clean up the strings?').lower()
    assert any(word in cleaned for word in ['strip','trim','whitespace'])
    default=send('What if an entry has no archived flag?').lower()
    assert any(word in default for word in ['default','missing','absent','not have','not present','not be filtered out','not filtered out','included','kept','considered active','treated as active','still active','remains active'])
    assert 'keyerror' in send('What happens if the title key is missing?').lower().replace('key error','keyerror')
    expected=send('What failure should a test expect in that case? Do not edit anything.').lower()
    assert 'keyerror' in expected.replace('key error','keyerror')
    window._load_conversation_list(select_id=general)
    assert window._chat_project() is None
    assert 'Docklight' in send('Remind me what I named my idea earlier.')
    window._new_chat();chat=window.current_conversation_id
    store.add_message(Message(None,chat,'user','what project are we on'))
    store.add_message(Message(None,chat,'assistant','project'))
    window._load_conversation_list(select_id=chat)
    repaired=send('uhh?',reaction=True).lower()
    assert 'general' in repaired or 'no project' in repaired
    assert (root/'notes.py').read_text()==code
    assert sorted(p.name for p in root.iterdir())==['README.md','notes.py']
