from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow


def test_default_start_is_general_and_new_chat_is_one_click(qtbot, tmp_path):
    store = ChatStore(tmp_path/'chat.db')
    project = store.upsert_project('neworld', str(tmp_path), 'python')
    store.set_setting('molicoder.selected_project_id', str(project.id))
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    window.show()
    assert window.current_project_id is None
    assert window.registry.selected() is None
    old = window.current_conversation_id
    window.composer.setPlainText('keep my draft')
    window._set_view('build')
    assert window.new_chat_button.isVisible()
    window.new_chat_button.click()
    assert window.current_conversation_id != old
    assert window.view_stack.currentWidget() == window.chat_view
    assert window.composer.toPlainText() == ''
    assert window._conversation_drafts[old][0] == 'keep my draft'


def test_explicit_project_restore_and_deselection(qtbot, tmp_path):
    store = ChatStore(tmp_path/'restore.db')
    project = store.upsert_project('neworld', str(tmp_path), 'python')
    store.set_setting('molicoder.selected_project_id', str(project.id))
    store.set_setting('ui.remember_workspace', 'true')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert window.current_project_id == project.id
    window.project_switch.setCurrentIndex(window.project_switch.findData(None))
    assert window.registry.selected() is None
    assert window.build_request.isEnabled()
