"""Semantic tokens and shared geometric component rules for both desktop themes."""
from string import Template

THEMES = {
    'default': dict(canvas='#101217', panel='#191D26', raised='#242B36', input='#111720',
        text='#F0F3FA', muted='#ADB9CC', border='#62748B', accent='#62E8F5',
        on_accent='#101217', selected='#263F50', magenta='#F79CE1', success='#ACED80',
        warning='#FFD17A', error='#FF9FAB', error_bg='#3B202B', code='#141C28'),
    'light': dict(canvas='#F3F1EA', panel='#FFFFFF', raised='#E7E5DD', input='#FFFFFF',
        text='#171C27', muted='#4F5C70', border='#536076', accent='#006575',
        on_accent='#FFFFFF', selected='#C9E9ED', magenta='#95266F', success='#306016',
        warning='#805000', error='#A4203C', error_bg='#FFE5EA', code='#E8EDF3'),
}


def normalize_theme(theme):
    # Previous Matrix/dark preferences retain their dark appearance.
    return 'light' if theme == 'light' else 'default'


def theme_tokens(theme):
    return THEMES[normalize_theme(theme)]


_COMPONENTS = Template(r'''
QWidget { background: $canvas; color: $text; font-family: "Segoe UI"; font-size: 17px; }
QLabel { background: transparent; }
QDialog, QMessageBox { background: $panel; }
QFrame#topbar { background: $panel; border-bottom: 3px solid $accent; }
QLabel#appTitle { font-size: 23px; font-weight: 800; letter-spacing: 1px; }
QLabel#brandDescriptor { color: $muted; font-size: 12px; font-weight: 700; letter-spacing: 1px; }
QLabel#brandLogo { background: #FFFFFF; border: 2px solid $border; }
QLabel#sectionTitle { color: $muted; font-size: 16px; font-weight: 700; letter-spacing: 1px; }
QLabel#projectHeading { font-size: 26px; font-weight: 750; }
QLabel#muted, QLabel.muted { color: $muted; }
QLabel#messageAuthor { font-weight: 750; }
QLabel#metricValue { color: $text; font-size: 22px; font-weight: 700; }
QLabel#modelBadge, QLabel#buildActivity { color: $success; font-weight: 650; }
QLabel#routeIdentity { color: $muted; background: $canvas; }
QLabel#welcome { color: $muted; border-left: 4px solid $magenta; padding: 18px; font-size: 18px; }
QFrame#sidebar, QFrame#inspector, QFrame#metricCard {
    background: $panel; border: 2px solid $border; border-radius: 0px;
}
QFrame#metricCard { border-top: 3px solid $border; }
QFrame#composerPanel { background: $panel; border: 2px solid $border; border-left: 5px solid $accent; }
QFrame#usageStrip { background: $raised; border-bottom: 1px solid $border; }
QFrame#drawerBar { background: $panel; border-top: 2px solid $border; }
QFrame#userBubble { background: $raised; border: 1px solid $border; border-right: 4px solid $magenta; }
QFrame#assistantBubble { background: $panel; border: 1px solid $border; border-left: 4px solid $accent; }
QFrame#reasoningPanel { background: $code; border: 1px solid $border; border-left: 3px solid $warning; }
QFrame#attachmentChip { background: $raised; border: 1px solid $border; }
QPushButton, QToolButton {
    background: $raised; color: $text; border: 2px solid $border; border-radius: 0px;
    padding: 6px 10px; font-weight: 650;
}
QPushButton:hover, QToolButton:hover { background: $selected; border-color: $accent; }
QPushButton:pressed, QToolButton:pressed { background: $selected; border-color: $magenta; }
QPushButton:checked { background: $selected; border-bottom: 4px solid $accent; }
QPushButton#primary, QPushButton#buildPrimary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 750; }
QPushButton#primary:hover, QPushButton#buildPrimary:hover { border-color: $text; }
QPushButton#danger { background: $error_bg; color: $error; border-color: $error; }
QPushButton#ghost { background: transparent; color: $muted; border: 2px solid transparent; }
QPushButton#ghost:hover { background: $selected; color: $text; border-color: $border; }
QPushButton:focus, QToolButton:focus, QPushButton#primary:focus, QPushButton#ghost:focus,
QPushButton#danger:focus { border: 2px solid $magenta; }
QPushButton:disabled, QToolButton:disabled, QPushButton#primary:disabled,
QPushButton#buildPrimary:disabled, QPushButton#danger:disabled, QPushButton#ghost:disabled {
    background: $canvas; color: $muted; border: 1px dashed $border;
}
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit, QTextBrowser {
    background: $input; color: $text; border: 1px solid $border; border-radius: 0px;
    padding: 7px; selection-background-color: $selected; selection-color: $text;
    placeholder-text-color: $muted;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: $accent; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QTextEdit:focus,
QTextBrowser:focus { border: 1px solid $magenta; }
QComboBox:disabled, QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled {
    background: $canvas; color: $muted; border: 1px dashed $border;
}
QComboBox QAbstractItemView { background: $panel; color: $text; border: 2px solid $border; selection-background-color: $selected; selection-color: $text; }
QListWidget, QTreeWidget, QTableWidget { background: $panel; color: $text; border: 1px solid $border; alternate-background-color: $raised; }
QListWidget::item, QTreeWidget::item { padding: 8px 6px; border-bottom: 1px solid $border; }
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected { background: $selected; color: $text; }
QListWidget::item:hover:!selected, QTreeWidget::item:hover:!selected { background: $raised; }
QListWidget:focus, QTreeWidget:focus, QTableWidget:focus { border-color: $magenta; }
QHeaderView::section { background: $raised; color: $text; padding: 6px; border: 1px solid $border; font-weight: 700; }
QScrollArea { border: none; background: $canvas; }
QScrollBar:vertical { background: $canvas; width: 10px; }
QScrollBar:horizontal { background: $canvas; height: 10px; }
QScrollBar::handle { background: $border; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: $accent; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QProgressBar { qproperty-textVisible: false; background: $input; border: 1px solid $border; min-height: 12px; text-align: center; color: $text; }
QProgressBar::chunk { background: $accent; }
QTabWidget::pane { background: $panel; border: 2px solid $border; }
QTabBar::tab { background: $raised; color: $muted; padding: 8px 16px; border: 1px solid $border; border-bottom: 3px solid $border; }
QTabBar::tab:selected { background: $panel; color: $text; border-bottom: 3px solid $accent; font-weight: 700; }
QTabBar::tab:hover { color: $text; border-bottom-color: $magenta; }
QGroupBox { border: 2px solid $border; margin-top: 14px; padding-top: 12px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; padding: 0 6px; }
QCheckBox, QRadioButton { background: transparent; spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 2px solid $border; background: $input; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: $accent; border: 4px solid $border; }
QCheckBox:focus, QRadioButton:focus { color: $magenta; }
QCheckBox:disabled, QRadioButton:disabled { color: $muted; }
QSplitter::handle { background: $border; }
QToolTip { background: $panel; color: $text; border: 2px solid $accent; padding: 6px; }
QMenu { background: $panel; color: $text; border: 2px solid $border; padding: 4px; }
QMenu::item { background: transparent; padding: 6px 24px 6px 12px; }
QMenu::item:selected { background: $selected; color: $text; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { background: $border; height: 1px; margin: 4px 8px; }
QMenuBar { background: $canvas; color: $muted; }
QMenuBar::item:selected { background: $selected; color: $text; }
QLabel#buildBlocker { color: $error; background: $error_bg; border: 2px solid $error; padding: 10px; font-weight: 700; }
''')


def stylesheet_for_theme(theme):
    return _COMPONENTS.substitute(theme_tokens(theme))


APP_STYLESHEET = stylesheet_for_theme('default')
