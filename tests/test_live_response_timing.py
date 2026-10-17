"""Opt-in real conversation through the desktop worker, driven entirely by CLI."""
import json
import os
from pathlib import Path
import time

import pytest

from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('MOLICODER_LIVE_TIMING') != '1', reason='set MOLICODER_LIVE_TIMING=1 for real model replies')]


def test_real_follow_up_replies_keep_their_response_times(qtbot, tmp_path):
    window = MainWindow(ChatStore(tmp_path / 'live-timing.db'), start_ollama_check=False)
    qtbot.addWidget(window)
    transcript = []
    for text in ('Remember my test idea is called Lantern. Keep your reply brief.', 'What did I call my test idea?'):
        started = time.monotonic()
        window.composer.setPlainText(text)
        window.send_button.click()
        qtbot.waitUntil(lambda: window.chat_worker is None, timeout=250000)
        reply = window._current_messages()[-1]
        receipt = window.store.message_receipt(reply.id)
        transcript.append({'input': text, 'answer': reply.content, 'receipt': receipt})
        report = os.environ.get('MOLICODER_TIMING_REPORT')
        if report:
            Path(report).write_text(json.dumps(transcript, indent=2), encoding='utf-8')
        assert receipt['outcome'] == 'completed', reply.content
        assert 'lantern' in reply.content.lower(), reply.content
        assert 0 < receipt['response_seconds'] <= time.monotonic() - started
        assert not window.transcript_selection._bubbles[-1].elapsed_label.isHidden()
    before = [b.elapsed_label.text() for b in window.transcript_selection._bubbles if b.role == 'assistant']
    window._load_conversation_list(select_id=window.current_conversation_id)
    assert [b.elapsed_label.text() for b in window.transcript_selection._bubbles if b.role == 'assistant'] == before
