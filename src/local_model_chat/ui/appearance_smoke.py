"""Explicit offscreen qualification of the packaged chat appearance controls."""
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QContextMenuEvent, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMenu

from ..domain import Message
from ..storage import ChatStore
from .main_window import MainWindow
from .settings_window import show_settings_window
from .theme import apply_theme


def appearance_smoke(app, report_path):
    report = {'status': 'failed', 'checks': []}
    window = None
    try:
        # The Windows offscreen platform needs explicit fonts to render text.
        if app.platformName() != 'offscreen':
            raise RuntimeError('Appearance qualification requires QT_QPA_PLATFORM=offscreen')
        for filename in ('segoeui.ttf', 'segoeuib.ttf', 'arial.ttf', 'consola.ttf'):
            font = Path('C:/Windows/Fonts') / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))

        def check(name, condition):
            report['checks'].append({'check': name, 'passed': bool(condition)})
            if not condition:
                raise RuntimeError(name)

        def settle():
            for _ in range(6):
                app.processEvents()
                time.sleep(.005)

        with tempfile.TemporaryDirectory(prefix='molicoder-appearance-') as directory:
            store = ChatStore(Path(directory) / 'profile.db')
            window = MainWindow(store, start_ollama_check=False)
            window.resize(1440, 900)
            for role, text in (
                ('user', 'What project are we working on?'),
                ('assistant', 'MoliCoder uses local tools and suitable models to inspect a project and check the result.'),
                ('user', 'Can we keep more of this conversation visible?'),
                ('assistant', 'Yes. Compact spacing reduces card padding and lets the composer grow with your draft.'),
            ):
                mid = store.add_message(Message(None, window.current_conversation_id, role, text))
                if role == 'assistant':
                    store.set_message_receipt(mid, {'provider': 'ollama', 'requested_model': 'molicoder:latest',
                        'model_source': 'requested', 'response_seconds': 8.4})
            window._render_messages(window._current_messages())
            window.show(); settle()
            bubbles = window.transcript_selection._bubbles
            check('compact cards fit short messages', all(b.height() < 110 for b in bubbles))
            check('empty composer does not reserve a large editor', window.composer.height() < 80)
            check('saved per-reply response times', all(b.elapsed_label.text() == '8.4s' for b in bubbles if b.role == 'assistant'))
            check('main page folder and project actions', window.open_folder_button.isVisible() and window.new_project_button.isVisible())
            for theme in ('light', 'default'):
                apply_theme(window, theme); settle()
                window.transcript_selection.select_all()
                expected = window.transcript_selection.selected_text()
                observed = []
                def inspect_menu():
                    menu = QApplication.activePopupWidget()
                    if not isinstance(menu, QMenu):
                        return
                    try:
                        observed.append(menu.grab().toImage().pixelColor(8, 8).lightness())
                        menu.grab().save(str(report_path.with_name(f'{report_path.stem}-{theme}-menu.png')))
                        action = next(a for a in menu.actions() if a.text() == 'Copy selection')
                        action.trigger()
                    finally:
                        menu.close()
                QTimer.singleShot(0, inspect_menu)
                view = bubbles[-1].body
                point = view.viewport().rect().center()
                app.sendEvent(view.viewport(), QContextMenuEvent(QContextMenuEvent.Mouse, point, view.viewport().mapToGlobal(point)))
                check(theme + ' actual context menu', len(observed) == 1 and (observed[0] > 220 if theme == 'light' else observed[0] < 45))
                check(theme + ' selection copied', bool(expected) and QApplication.clipboard().text() == expected)
                window.transcript_selection.clear()
                window.grab().save(str(report_path.with_name(f'{report_path.stem}-{theme}-1440.png')))
            apply_theme(window, 'light'); show_settings_window(window); settle()
            family = 'Arial' if 'Arial' in QFontDatabase.families() else QFontDatabase.families()[0]
            window._settings_font_control.setCurrentFont(QFont(family))
            window._settings_text_size_control.setValue(19)
            window._settings_density_control.setCurrentIndex(1); settle()
            check('font and size applied to existing text', bubbles[-1].body.font().family() == family and bubbles[-1].body.font().pixelSize() == 19)
            check('preferences saved immediately', store.get_setting('ui.font_family') == family and store.get_setting('ui.text_size') == '19' and store.get_setting('ui.chat_density') == 'comfortable')
            window._settings_dialog.grab().save(str(report_path.with_name(f'{report_path.stem}-settings.png')))
            window._settings_density_control.setCurrentIndex(0)
            window._settings_text_size_control.setValue(17)
            window._settings_dialog.close(); window.resize(1020, 680); settle()
            check('folder and project controls fit minimum window', window.new_project_button.geometry().right() < window.width() and window.chat_switch.width() >= 100)
            window.grab().save(str(report_path.with_name(f'{report_path.stem}-light-1020.png')))
            from .plan_review import PlanReviewDialog
            tasks = [{'key': f'task-{i}', 'title': f'Implement and verify feature {i}',
                      'objective': 'Detailed implementation scope. ' * 30,
                      'acceptance_criteria': ['Expected behavior is verified.']} for i in range(1, 21)]
            plan = {'id': 1, 'payload': {'objective': 'Review a complete plan before execution.', 'tasks': tasks,
                'task_assignments': {t['key']: {'provider': 'fixture', 'model': 'review-model', 'effort': 'high'} for t in tasks}}}
            for theme in ('default', 'light'):
                apply_theme(window, theme)
                review = PlanReviewDialog(window, plan=plan, run={'provider': 'fixture', 'model': 'review-model'},
                    detail='Full revision details', can_edit=True)
                review.show(); settle()
                screen = review.screen().availableGeometry()
                check(theme + ' plan stays inside screen', review.width() < screen.width() and review.height() < screen.height())
                check(theme + ' tasks scroll', review.tasks.verticalScrollBar().maximum() > 0)
                review.grab().save(str(report_path.with_name(f'{report_path.stem}-plan-{theme}.png')))
                review.show_task(review.tasks.item(0)); settle()
                check(theme + ' task details readable', 'Expected behavior is verified.' in review.details.toPlainText())
                review.back.click()
                check(theme + ' task back navigation', review.pages.currentIndex() == 0)
                review.close()
                check(theme + ' close never approves', review.decision == 'dismiss')
            window.close()
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        if window is not None:
            if hasattr(window, '_settings_dialog'):
                window._settings_dialog.close()
            window.close()
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1
