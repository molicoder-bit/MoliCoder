"""Apply and persist the application theme without rebuilding the workspace."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel
from .styles import stylesheet_for_theme


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
    if light:
        sheet += 'QLabel#modelBadge, QLabel#buildActivity { color: #216442; } QLabel#brandLogo { background: #FFFFFF; }'
    app.setStyleSheet(sheet)
    window.theme_button.setText('Matrix' if light else 'Light')
    window.theme_button.setAccessibleName('Switch to Matrix theme' if light else 'Switch to Light theme')
    window.theme_button.setToolTip('Current theme: ' + theme)
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
