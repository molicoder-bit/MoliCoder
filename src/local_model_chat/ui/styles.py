APP_STYLESHEET = r"""
QWidget {
    background: #0B0F14;
    color: #F8FAFC;
    font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
    font-size: 14px;
}
QMainWindow { background: #080B10; }
QFrame#sidebar, QFrame#inspector {
    background: #0F1623;
    border: 1px solid #1E293B;
}
QFrame#topbar {
    background: #0B0F14;
    border-bottom: 1px solid #1E293B;
}
QLabel#appTitle { font-size: 20px; font-weight: 700; color: #FFFFFF; }
QLabel#muted, QLabel.muted { color: #94A3B8; }
QLabel#sectionTitle { color: #CBD5E1; font-size: 12px; font-weight: 700; }
QLabel#metricValue { font-size: 17px; font-weight: 650; color: #F8FAFC; }
QPushButton, QToolButton {
    background: #172033;
    border: 1px solid #2B3A55;
    border-radius: 8px;
    padding: 8px 12px;
    color: #E2E8F0;
    font-weight: 600;
}
QPushButton:hover, QToolButton:hover { background: #21304A; border-color: #3B82F6; }
QPushButton:pressed, QToolButton:pressed { background: #0F172A; }
QPushButton:disabled { color: #64748B; background: #111827; border-color: #1E293B; }
QPushButton#primary {
    background: #06B6D4;
    border-color: #22D3EE;
    color: #03151B;
    font-weight: 750;
}
QPushButton#primary:hover { background: #22D3EE; }
QPushButton#danger { color: #FDA4AF; border-color: #7F1D1D; background: #2A1118; }
QPushButton#ghost { background: transparent; border-color: transparent; color: #94A3B8; }
QPushButton#ghost:hover { background: #172033; color: #F8FAFC; }
QComboBox, QSpinBox, QDoubleSpinBox {
    background: #111827;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 8px;
    selection-background-color: #164E63;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #22D3EE; }
QComboBox QAbstractItemView { background: #111827; border: 1px solid #334155; selection-background-color: #164E63; }
QLineEdit, QTextEdit, QTextBrowser {
    background: #0F1623;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 9px;
    selection-background-color: #155E75;
}
QLineEdit:focus, QTextEdit:focus { border-color: #22D3EE; }
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
QListWidget::item:hover:!selected { background: #172033; }
QScrollArea { border: none; background: #0B0F14; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #334155; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar {
    background: #111827;
    border: 1px solid #273449;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background: #22D3EE; border-radius: 5px; }
QFrame#metricCard {
    background: #111827;
    border: 1px solid #223047;
    border-radius: 10px;
}
QFrame#userBubble {
    background: #12344A;
    border: 1px solid #155E75;
    border-radius: 12px;
}
QFrame#assistantBubble {
    background: #111827;
    border: 1px solid #273449;
    border-radius: 12px;
}
QFrame#reasoningPanel {
    background: #21170A;
    border: 1px solid #78450A;
    border-radius: 8px;
}
QSplitter::handle { background: #1E293B; width: 1px; }
QToolTip { background: #111827; color: #F8FAFC; border: 1px solid #475569; padding: 6px; }
"""

