"""Apply and persist the application theme without rebuilding the workspace."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel
from .styles import stylesheet_for_theme

DEFAULT_TEXT_SIZE = 17
MIN_TEXT_SIZE = 12
MAX_TEXT_SIZE = 22


def current_text_size(window):
    try:
        value = int(window.store.get_setting('ui.text_size', str(DEFAULT_TEXT_SIZE)))
    except ValueError:
        value = DEFAULT_TEXT_SIZE
    return max(MIN_TEXT_SIZE, min(MAX_TEXT_SIZE, value))


def set_text_size(window, value):
    value = max(MIN_TEXT_SIZE, min(MAX_TEXT_SIZE, int(value)))
    window.store.set_setting('ui.text_size', str(value))
    control = getattr(window, '_settings_text_size_control', None)
    if control is not None and control.value() != value:
        control.blockSignals(True)
        control.setValue(value)
        control.blockSignals(False)
    apply_theme(window, window.theme_selector.currentData(), persist=False)


def theme_color(role):
    light = QApplication.instance().property('molicoderTheme') == 'light'
    colors = {
        'accent': ('#216442', '#00EE24'),
        'muted': ('#536650', '#69B875'),
        'success': ('#216442', '#00EE24'),
        'warning': ('#8A5409', '#F59E0B'),
        'error': ('#B02743', '#FB7185'),
    }
    return colors[role][0 if light else 1]


def apply_theme(window, theme, *, persist=True):
    theme = 'matrix' if theme == 'dark' else theme
    theme = theme if theme in {'matrix', 'light'} else 'matrix'
    app = QApplication.instance()
    app.setProperty('molicoderTheme', theme)
    palette = QPalette()
    light = theme == 'light'
    for role, color in {
        QPalette.Window: '#F6F7F2' if light else '#000000',
        QPalette.WindowText: '#202C23' if light else '#85FF9C',
        QPalette.Base: '#FFFFFF' if light else '#020A03',
        QPalette.AlternateBase: '#E7EEE3' if light else '#041508',
        QPalette.Text: '#202C23' if light else '#85FF9C',
        QPalette.Button: '#E7EEE3' if light else '#041508',
        QPalette.ButtonText: '#202C23' if light else '#85FF9C',
        QPalette.Highlight: '#CEE6CD' if light else '#07551A',
        QPalette.HighlightedText: '#202C23' if light else '#A3FFB0',
        QPalette.PlaceholderText: '#61755F' if light else '#69B875',
        QPalette.Link: '#216442' if light else '#00EE24',
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    sheet = stylesheet_for_theme(theme)
    text_size = current_text_size(window)
    sheet += f'QWidget {{ font-size: {text_size}px; }}'
    if light:
        sheet += 'QLabel#modelBadge, QLabel#buildActivity { color: #216442; } QLabel#brandLogo { background: #FFFFFF; }'
    app.setStyleSheet(sheet)
    window.theme_selector.blockSignals(True)
    window.theme_selector.setCurrentIndex(window.theme_selector.findData(theme))
    window.theme_selector.blockSignals(False)
    window.theme_selector.setToolTip('Current theme: ' + window.theme_selector.currentText())
    from .widgets import ChatBubble
    for bubble in window.findChildren(ChatBubble):
        bubble.apply_theme()
    for label in window.findChildren(QLabel):
        tone = label.property('themeTone')
        if tone:
            label.setStyleSheet(f'color: {theme_color(tone)}; font-weight: 650;')
    window._refresh_context_meter()
    if persist:
        window.store.set_setting('ui.theme', theme)
