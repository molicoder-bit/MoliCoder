"""Screen-bounded plan review with a task overview and drill-down details."""
from html import escape
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)


class PlanReviewDialog(QDialog):
    def __init__(self, parent, *, plan, run, detail, can_edit):
        super().__init__(parent)
        self.decision = 'dismiss'
        self.setWindowTitle('Review Build plan')
        self.setSizeGripEnabled(True)
        area = self.screen().availableGeometry()
        self.resize(min(940, area.width()-40), min(720, area.height()-80))
        self.setMaximumSize(max(300, area.width()-20), max(300, area.height()-60))
        outer = QVBoxLayout(self)
        payload = plan['payload']
        title = QLabel(f"Plan revision {plan['id']} · {len(payload.get('tasks', []))} tasks")
        title.setObjectName('projectHeading'); outer.addWidget(title)
        self.pages = QStackedWidget(); outer.addWidget(self.pages, 1)
        overview = QWidget(); layout = QVBoxLayout(overview); layout.setContentsMargins(0,0,0,0)
        summary = QTextBrowser(); summary.setOpenExternalLinks(False)
        summary.setMaximumHeight(180)
        summary.setPlainText(payload.get('objective', '') + '\n\nPlanning: ' + run['provider'] + ' / ' + (run['model'] or 'provider default') +
            '\n' + str(payload.get('runner_policy', {}).get('selection_reason', '')) +
            '\n' + '\n'.join(payload.get('review_warnings', [])) +
            ('\nRecovery: ' + run['recovery_reason'] if run.get('recovery_reason') else ''))
        layout.addWidget(summary)
        self.tasks = QListWidget(); self.tasks.setWordWrap(True)
        self.tasks.setResizeMode(QListView.ResizeMode.Adjust)
        self.tasks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tasks.setAccessibleName('Plan tasks: number, title, model and effort')
        for number, task in enumerate(payload.get('tasks', []), 1):
            assignment = payload.get('task_assignments', {}).get(task.get('key'), {})
            complexity = assignment.get('complexity', task.get('complexity', 'unrated'))
            item = QListWidgetItem(f"{number:02d}   {task.get('title', '')}\n       {assignment.get('provider') or assignment.get('runner', 'unassigned')} / {assignment.get('model') or 'provider default'} · {assignment.get('effort') or 'provider-managed effort'} · {complexity} complexity")
            item.setData(Qt.ItemDataRole.UserRole, (task, assignment))
            self.tasks.addItem(item)
        layout.addWidget(self.tasks, 1)
        self.pages.addWidget(overview)
        self.details = QTextBrowser(); self.details.setOpenExternalLinks(False)
        self.details.setAccessibleName('Selected task details'); self.pages.addWidget(self.details)
        nav = QHBoxLayout(); outer.addLayout(nav)
        self.back = QPushButton('Back to tasks'); self.back.hide(); nav.addWidget(self.back)
        self.back.clicked.connect(lambda: (self.pages.setCurrentIndex(0), self.back.hide()))
        full = QPushButton('Full plan and diagnostics'); nav.addWidget(full)
        full.clicked.connect(lambda: (self.details.setPlainText(detail), self.pages.setCurrentIndex(1), self.back.show()))
        nav.addStretch()
        actions = QHBoxLayout(); outer.addLayout(actions)
        note = QLabel('Approve this revision for isolated execution. Provider charges may apply.'); note.setWordWrap(True); outer.addWidget(note)
        if can_edit:
            edit = QPushButton('Edit JSON'); actions.addWidget(edit); edit.clicked.connect(lambda: self.choose('edit'))
        actions.addStretch()
        reject = QPushButton('Reject plan'); actions.addWidget(reject); reject.clicked.connect(lambda: self.choose('reject'))
        approve = QPushButton('Approve plan'); approve.setObjectName('primary'); actions.addWidget(approve)
        approve.clicked.connect(lambda: self.choose('approve'))
        reject.setDefault(True)
        self.tasks.itemClicked.connect(self.show_task)
        self.tasks.itemActivated.connect(self.show_task)

    def choose(self, decision):
        self.decision = decision
        self.accept()

    def show_task(self, item):
        task, assignment = item.data(Qt.ItemDataRole.UserRole)
        fields = [('Objective', task.get('objective', '')), ('Model', assignment.get('model') or 'provider default'),
            ('Execution complexity', assignment.get('complexity', task.get('complexity', 'Not recorded'))),
            ('Fallback', (assignment.get('fallback_provider') or 'None') + ' / ' + (assignment.get('fallback_model') or 'None')),
            ('Effort', assignment.get('effort') or 'provider managed'), ('Selection reason', assignment.get('reason', '')),
            ('Dependencies', task.get('depends_on', [])), ('Files', task.get('expected_paths', [])),
            ('Acceptance', task.get('acceptance_criteria', [])), ('Checks', task.get('verification_checks', [])),
            ('Artifact checks', task.get('artifact_checks', []))]
        html = '<h2>' + escape(task.get('title', 'Task')) + '</h2>'
        for label, value in fields:
            text = '\n'.join(map(str, value)) if isinstance(value, list) else str(value)
            html += '<h3>' + label + '</h3><p>' + escape(text or 'None').replace('\n','<br>') + '</p>'
        self.details.setHtml(html); self.pages.setCurrentIndex(1); self.back.show()
