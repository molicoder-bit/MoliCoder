from PySide6.QtWidgets import QApplication, QMenu
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.widgets import ChatBubble
from local_model_chat.ui.theme import DEFAULT_TEXT_SIZE, current_text_size


def test_theme_switch_is_immediate_and_preserves_drafts(qtbot, tmp_path):
    store = ChatStore(tmp_path/'theme.db')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert QApplication.instance().property('molicoderTheme') == 'matrix'
    assert window.theme_selector.currentData() == 'matrix'
    assert [window.theme_selector.itemText(i) for i in range(window.theme_selector.count())] == ['Matrix', 'Light']
    window.composer.setPlainText('Keep this chat draft')
    window.build_request.setPlainText('Keep this build draft')
    bubble = ChatBubble('assistant', '> Quoted project evidence', parent=window)
    window.theme_selector.setCurrentIndex(window.theme_selector.findData('light'))
    assert QApplication.instance().property('molicoderTheme') == 'light'
    assert store.get_setting('ui.theme') == 'light'
    assert window.theme_selector.currentText() == 'Light'
    assert '#F6F7F2' in QApplication.instance().styleSheet()
    assert '#202C23' in bubble.body.document().defaultStyleSheet()
    assert window.composer.toPlainText() == 'Keep this chat draft'
    assert window.build_request.toPlainText() == 'Keep this build draft'
    window.theme_selector.setCurrentIndex(window.theme_selector.findData('matrix'))
    assert store.get_setting('ui.theme') == 'matrix'
    assert '#85FF9C' in bubble.body.document().defaultStyleSheet()


def test_saved_theme_restores_without_rewriting_preference(qtbot, tmp_path):
    store = ChatStore(tmp_path/'saved-theme.db')
    store.set_setting('ui.theme', 'light')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert QApplication.instance().property('molicoderTheme') == 'light'
    assert window.theme_selector.currentText() == 'Light'
    assert store.get_setting('ui.theme') == 'light'


def test_invalid_theme_falls_back_to_matrix(qtbot, tmp_path):
    store = ChatStore(tmp_path/'invalid-theme.db')
    store.set_setting('ui.theme', 'unrecognized')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert QApplication.instance().property('molicoderTheme') == 'matrix'
    assert window.theme_selector.currentText() == 'Matrix'


def test_view_text_size_controls_cover_header_and_editor(qtbot, tmp_path):
    store = ChatStore(tmp_path/'text-size.db')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert current_text_size(window) == DEFAULT_TEXT_SIZE == 17
    for widget in (window.theme_selector, window.project_switch, window.composer):
        assert widget.font().pixelSize() == 17

    view_menu = next(menu for menu in window.menuBar().findChildren(QMenu) if menu.title() == 'View')
    actions = {action.text(): action for action in view_menu.actions()}
    actions['Increase text size'].trigger()
    assert current_text_size(window) == 18
    assert store.get_setting('ui.text_size') == '18'
    assert window.theme_selector.font().pixelSize() == 18
    assert window.composer.font().pixelSize() == 18
    actions['Decrease text size'].trigger()
    assert current_text_size(window) == 17
    actions['Reset text size'].trigger()
    assert store.get_setting('ui.text_size') == '17'
