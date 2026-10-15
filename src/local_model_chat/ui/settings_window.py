"""Persistent, navigable settings with everyday actions first."""
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QVBoxLayout, QListWidget,
    QStackedWidget, QWidget, QLabel, QPushButton, QCheckBox, QComboBox, QScrollArea)


CATEGORIES = ('General', 'Models & providers', 'Projects & Build', 'Usage & limits', 'Kandev', 'Diagnostics')


def show_settings_window(w, category='General'):
    if not hasattr(w, '_settings_dialog'):
        dialog = QDialog(w)
        dialog.setWindowTitle('Settings')
        dialog.resize(900, 640)
        layout = QHBoxLayout(dialog)
        nav = QListWidget(); nav.addItems(CATEGORIES); nav.setMaximumWidth(220)
        pages = QStackedWidget()
        layout.addWidget(nav); layout.addWidget(pages, 1)
        descriptions = (
            'App preferences · Changes are saved immediately.',
            'Choose the model for your next Chat reply. External agents and API connections have separate supported uses.',
            'Choose a folder for repository work and review how Build runs.',
            'Review measured usage and set project limits. Subscription agent usage may be unavailable.',
            'Optional task board and agent ownership. Choose Kandev deliberately for each Build.',
            'Inspect local runtime health, installation readiness, and run evidence.',
        )
        for name, description in zip(CATEGORIES, descriptions):
            page = QWidget(); body = QVBoxLayout(page)
            title = QLabel(name); title.setObjectName('appTitle'); body.addWidget(title)
            note = QLabel(description); note.setWordWrap(True); body.addWidget(note)
            def action(label, callback):
                button = QPushButton(label); button.clicked.connect(callback); body.addWidget(button)
            if name == 'General':
                theme = QComboBox(); theme.addItems(['Matrix', 'Light'])
                theme.setCurrentIndex(w.theme_selector.currentIndex())
                theme.currentIndexChanged.connect(w.theme_selector.setCurrentIndex)
                body.addWidget(QLabel('Appearance')); body.addWidget(theme)
                for label, key in [('Remember last workspace on launch', 'ui.remember_workspace'), ('Reduce animation', 'ui.reduce_motion')]:
                    check = QCheckBox(label); check.setChecked(w.store.get_setting(key, 'false') == 'true')
                    check.toggled.connect(lambda value, setting=key: w.store.set_setting(setting, str(value).lower()))
                    body.addWidget(check)
            elif name == 'Models & providers':
                body.addWidget(QLabel('Local Chat model · App/next reply'))
                body.addWidget(w.model_combo); w.model_combo.show()
                detail = QLabel('Changing a model can open a new chat. Previous replies keep their recorded origin.'); detail.setWordWrap(True); body.addWidget(detail)
                action('Install or inspect local models', w._show_environment_doctor)
                action('Connect an account or configure Build models', w._provider_settings)
                body.addWidget(QLabel('Advanced generation controls and context estimates are in Performance.'))
            elif name == 'Projects & Build':
                project = w.registry.selected()
                scope = QLabel(f'Project: {project.name}\n{project.path}' if project else 'No project selected'); scope.setWordWrap(True); body.addWidget(scope)
                action('Open project folder', w._add_project)
                action('Create project', w._create_project)
                action('Project knowledge and decisions', w._open_knowledge_review)
                explanation = QLabel('Local offline uses your local model for eligible edits. Hybrid can hand work to your chosen external agent. Frontier only uses that external agent. Build asks for the required project permission before an agent runs.'); explanation.setWordWrap(True); body.addWidget(explanation)
            elif name == 'Usage & limits':
                action('Review project usage and set limits', w._show_usage_budgets)
            elif name == 'Kandev':
                action('Connect or configure Kandev', w.kandev_panel.configure)
                action('Open board', lambda: (w._set_view('build'), w.build_tabs.setCurrentIndex(1), dialog.hide()))
            else:
                action('Check environment and installation readiness', w._show_environment_doctor)
                action('Export task evidence and logs', w._show_task_evidence)
                action('Inspect local runtime performance', lambda: (dialog.hide(), __import__('local_model_chat.ui.workspace', fromlist=['open_drawer']).open_drawer(w, 'Performance')))
            body.addStretch()
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); pages.addWidget(scroll)
        nav.currentRowChanged.connect(pages.setCurrentIndex)
        w._settings_dialog, w._settings_nav = dialog, nav
    w._settings_nav.setCurrentRow(CATEGORIES.index(category) if category in CATEGORIES else 0)
    w._settings_dialog.show(); w._settings_dialog.raise_()
