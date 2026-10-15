APP_STYLESHEET = r"""
QWidget {
    background: #151816;
    color: #EAE9E1;
    font-family: "Segoe UI";
    font-size: 14px;
}
QMainWindow { background: #111512; }
QFrame#sidebar, QFrame#inspector {
    background: #1E2421;
    border: 1px solid #303A33;
}
QFrame#topbar {
    background: #151816;
    border-bottom: 1px solid #303A33;
}
QLabel#appTitle { font-size: 20px; font-weight: 700; color: #FFFFFF; }
QLabel#muted, QLabel.muted { color: #A2B2A6; }
QLabel#sectionTitle { color: #CBD5E1; font-size: 12px; font-weight: 700; }
QLabel#metricValue { font-size: 17px; font-weight: 650; color: #EAE9E1; }
QPushButton, QToolButton {
    background: #26312B;
    border: 1px solid #3E4D42;
    border-radius: 8px;
    padding: 8px 12px;
    color: #EAE9E1;
    font-weight: 600;
}
QPushButton:hover, QToolButton:hover { background: #334438; border-color: #9AE6BE; }
QPushButton:pressed, QToolButton:pressed { background: #18241D; }
QPushButton:disabled { color: #64748B; background: #19221C; border-color: #303A33; }
QPushButton#primary {
    background: #9AE6BE;
    border-color: #9AE6BE;
    color: #102419;
    font-weight: 750;
}
QPushButton#primary:hover { background: #9AE6BE; }
QPushButton#danger { color: #FDA4AF; border-color: #7F1D1D; background: #2A1118; }
QPushButton#ghost { background: transparent; border-color: transparent; color: #A2B2A6; }
QPushButton#ghost:hover { background: #26312B; color: #EAE9E1; }
QComboBox, QSpinBox, QDoubleSpinBox {
    background: #19221C;
    border: 1px solid #3B4940;
    border-radius: 8px;
    padding: 8px;
    selection-background-color: #355440;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #9AE6BE; }
QComboBox QAbstractItemView { background: #19221C; border: 1px solid #3B4940; selection-background-color: #355440; }
QLineEdit, QTextEdit, QTextBrowser {
    background: #1E2421;
    border: 1px solid #3B4940;
    border-radius: 10px;
    padding: 9px;
    selection-background-color: #3F6149;
}
QLineEdit:focus, QTextEdit:focus { border-color: #9AE6BE; }
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    background: transparent;
    color: #CBD5E1;
    padding: 10px 8px;
    margin: 2px 0;
    border-radius: 8px;
}
QListWidget::item:selected { background: #17314A; color: #FFFFFF; }
QListWidget::item:hover:!selected { background: #26312B; }
QScrollArea { border: none; background: #151816; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #3B4940; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar {
    background: #19221C;
    border: 1px solid #273449;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background: #9AE6BE; border-radius: 5px; }
QFrame#metricCard {
    background: #19221C;
    border: 1px solid #223047;
    border-radius: 10px;
}
QFrame#userBubble {
    background: #12344A;
    border: 1px solid #3F6149;
    border-radius: 12px;
}
QFrame#assistantBubble {
    background: #19221C;
    border: 1px solid #273449;
    border-radius: 12px;
}
QFrame#reasoningPanel {
    background: #21170A;
    border: 1px solid #78450A;
    border-radius: 8px;
}
QSplitter::handle { background: #303A33; width: 1px; }
QToolTip { background: #19221C; color: #EAE9E1; border: 1px solid #475569; padding: 6px; }
"""


APP_STYLESHEET += r"""
QLabel#brandLogo { background: #EAE9E1; border-radius: 10px; }
QFrame#usageStrip { background: #1A211C; border-bottom: 1px solid #303A33; }
QFrame#drawerBar { background: #111512; border-top: 1px solid #303A33; }
QFrame#assistantBubble { background: transparent; border: none; border-left: 2px solid #355440; border-radius: 0; }
QFrame#userBubble { background: #26312B; border: none; border-radius: 8px; }
QTabWidget::pane { border: 1px solid #303A33; }
QTabBar::tab { background: #19221C; color: #A2B2A6; padding: 9px 18px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #EAE9E1; border-bottom: 2px solid #F0AF7B; }
QPushButton:checked { border-color: #9AE6BE; background: #355440; }
QScrollArea { border: none; }
"""

APP_STYLESHEET += "QPushButton#buildPrimary { background: #F0AF7B; color: #26190F; border-color: #F0AF7B; } QLabel { background: transparent; }"
