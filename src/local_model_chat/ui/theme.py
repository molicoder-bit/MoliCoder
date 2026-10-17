"""Apply and persist the application theme without rebuilding the workspace."""
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QLabel
from .styles import stylesheet_for_theme, theme_tokens, normalize_theme

DEFAULT_TEXT_SIZE = 17
MIN_TEXT_SIZE = 12
MAX_TEXT_SIZE = 32
DEFAULT_FONT_FAMILY = 'Segoe UI'


def current_font_family(window):
    family = window.store.get_setting('ui.font_family', DEFAULT_FONT_FAMILY)
    return family if family in QFontDatabase.families() else DEFAULT_FONT_FAMILY


def set_font_family(window, family):
    if family not in QFontDatabase.families():
        return
    window.store.set_setting('ui.font_family', family)
    apply_theme(window, window.theme_selector.currentData(), persist=False)


def compact_chat(window):
    return window.store.get_setting('ui.chat_density', 'compact') != 'comfortable'


def set_chat_density(window, density):
    window.store.set_setting('ui.chat_density', 'comfortable' if density == 'comfortable' else 'compact')
    apply_chat_density(window)


def apply_chat_density(window):
    from .widgets import ChatBubble
    compact = compact_chat(window)
    QApplication.instance().setProperty('molicoderCompactChat', compact)
    window.transcript_layout.setSpacing(6 if compact else 12)
    for bubble in window.findChildren(ChatBubble):
        bubble.set_compact(compact)
    window.composer.set_compact(compact)


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
    return theme_tokens(QApplication.instance().property('molicoderTheme'))[role]


def apply_theme(window, theme, *, persist=True):
    theme = normalize_theme(theme)
    app = QApplication.instance()
    app.setProperty('molicoderTheme', theme)
    palette = QPalette()
    tokens = theme_tokens(theme)
    for role, key in {
        QPalette.Window: 'canvas', QPalette.WindowText: 'text', QPalette.Base: 'input',
        QPalette.AlternateBase: 'raised', QPalette.Text: 'text', QPalette.Button: 'raised',
        QPalette.ButtonText: 'text', QPalette.Highlight: 'selected', QPalette.HighlightedText: 'text',
        QPalette.PlaceholderText: 'muted', QPalette.Link: 'accent', QPalette.LinkVisited: 'magenta',
        QPalette.ToolTipBase: 'panel', QPalette.ToolTipText: 'text',
    }.items():
        palette.setColor(role, QColor(tokens[key]))
    app.setPalette(palette)
    sheet = stylesheet_for_theme(theme)
    text_size = current_text_size(window)
    family = current_font_family(window)
    font = QFont(family)
    font.setPixelSize(text_size)
    app.setFont(font)
    escaped_family = family.replace('\\', '\\\\').replace('"', '\\"')
    sheet += f'QWidget {{ font-family: "{escaped_family}"; font-size: {text_size}px; }}'
    app.setStyleSheet(sheet)
    apply_chat_density(window)
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
