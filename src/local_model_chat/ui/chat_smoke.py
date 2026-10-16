"""Typed Chat journey for the packaged smoke check's isolated fixture database."""
from pathlib import Path
import time


def verify_local_chat(window, project_id, conversation_id, root, app):
    folder = Path(root) / 'folder listing fixture'
    folder.mkdir()
    (folder / 'visible.txt').write_text('File contents must not be read.', encoding='utf-8')
    (folder / 'nested').mkdir()
    (folder / 'nested' / 'hidden-child.txt').write_text('Nested contents', encoding='utf-8')
    window.store.link_conversation_project(conversation_id, project_id)
    window._load_conversation_list(select_id=conversation_id)
    window.composer.setPlainText("good, do you know what project we're working on?")
    window.send_button.click()
    answer = window.store.list_messages(conversation_id)[-1]
    receipt = window.store.message_receipt(answer.id)
    if (window.chat_worker is not None or receipt.get('model_source') != 'no_inference'
            or window.store.get_project(project_id).name not in answer.content):
        raise RuntimeError('Packaged Chat did not answer project identity from saved records')

    # Preserve the reported wording; only the path changes to an isolated fixture.
    message = f'Ok that\'s good, can you tell me the contents of the folder "{folder}"?'
    window.composer.setPlainText(message)
    window.send_button.click()
    worker = window.chat_worker
    if not getattr(worker, 'local_read', False):
        if worker is not None:
            worker.cancel()
            worker.wait(5000)
        raise RuntimeError('Packaged folder query fell through to model inference')
    deadline = time.monotonic() + 10
    while window.chat_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    if window.chat_worker is not None:
        worker.cancel()
        worker.wait(5000)
        raise RuntimeError('Packaged folder listing did not finish')
    answer = window.store.list_messages(conversation_id)[-1]
    receipt = window.store.message_receipt(answer.id)
    if (receipt.get('model_source') != 'no_inference' or receipt.get('outcome') != 'completed'
            or receipt.get('entries') != 2 or Path(receipt['target']).resolve() != folder.resolve()
            or 'visible.txt' not in answer.content or 'hidden-child.txt' in answer.content
            or window.current_conversation_id != conversation_id):
        raise RuntimeError('Packaged Chat folder answer or saved receipt is incorrect')
