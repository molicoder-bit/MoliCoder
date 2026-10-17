"""Task result review, with explicit acceptance and a separate repair action."""
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QScrollArea, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)


class TaskReviewDialog(QDialog):
    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.decision = 'dismiss'
        self.snapshot = snapshot
        self.setWindowTitle('Review task & continue')
        self.setSizeGripEnabled(True)
        area = self.screen().availableGeometry()
        self.resize(min(900,area.width()-40),min(760,area.height()-70))
        self.setMaximumSize(max(300,area.width()-20),max(300,area.height()-40))
        layout = QVBoxLayout(self)
        title = QLabel(snapshot['title']); title.setObjectName('projectHeading'); title.setWordWrap(True)
        layout.addWidget(title)
        tabs = QTabWidget(); layout.addWidget(tabs,1)
        page = QWidget(); content = QVBoxLayout(page)
        reason = QLabel(snapshot['reason']); reason.setWordWrap(True); content.addWidget(reason)
        hint = QLabel('Confirm the requirements you checked. Accepting keeps this result and continues remaining tasks; it does not rerun this task.')
        hint.setWordWrap(True); content.addWidget(hint)
        self.checks = []
        for criterion in snapshot['criteria']:
            label = QLabel(criterion); label.setWordWrap(True); content.addWidget(label)
            check = QCheckBox('I verified this requirement'); check.setAccessibleName(criterion)
            check.setProperty('criterion',criterion)
            content.addWidget(check); self.checks.append(check)
        error = snapshot['errors'].get('approve')
        if error:
            label = QLabel(error); label.setWordWrap(True); content.addWidget(label)
        evidence = QLabel('Files: ' + (', '.join(snapshot['files']) or 'No verified files') + '\nCheckpoint: ' + snapshot['head'])
        evidence.setWordWrap(True); content.addWidget(evidence)
        content.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        tabs.addTab(scroll,'Review requirements')
        validation = QTextBrowser(); validation.setPlainText(snapshot['validation']); tabs.addTab(validation,'Checks')
        diff = QTextBrowser(); diff.setPlainText(('Preview limited to 100,000 characters. Open the worktree for the full diff.\n\n' if snapshot['diff_truncated'] else '') + snapshot['diff'])
        tabs.addTab(diff,'Run changes')
        routes = QTextBrowser()
        lines = ['Remaining tasks use these assignments. Accepting skips the reviewed task; requesting changes retries it.', '']
        payload = snapshot['plans'].get('changes') or snapshot['plans'].get('approve') or {}
        for task in payload.get('tasks',[]):
            if task['key'] in snapshot['completed_keys']:
                continue
            assignment = payload.get('task_assignments',{}).get(task['key'],{})
            prefix = 'Retry only — ' if task['key']==snapshot['key'] else ''
            lines.append(f"{prefix}{task['title']}: {assignment.get('provider')} / {assignment.get('model')} · {assignment.get('effort') or 'provider-managed effort'}")
        routes.setPlainText('\n'.join(lines)); tabs.addTab(routes,'Execution models')
        self.feedback = QPlainTextEdit(); self.feedback.setPlaceholderText('Describe what should change, or how the blocker was resolved…')
        self.feedback.setAccessibleName('Requested changes'); self.feedback.setMaximumHeight(95); layout.addWidget(self.feedback)
        if snapshot['errors'].get('changes'):
            error_label = QLabel(snapshot['errors']['changes']); error_label.setWordWrap(True); layout.addWidget(error_label)
        actions = QHBoxLayout(); layout.addLayout(actions)
        later = QPushButton('Review later'); later.clicked.connect(self.reject); actions.addWidget(later)
        actions.addStretch()
        self.changes = QPushButton('Request changes & retry'); self.changes.clicked.connect(lambda:self.choose('changes')); actions.addWidget(self.changes)
        self.approve = QPushButton('Accept & continue'); self.approve.setObjectName('primary'); self.approve.clicked.connect(lambda:self.choose('approve')); actions.addWidget(self.approve)
        self.feedback.textChanged.connect(self.refresh)
        for check in self.checks: check.toggled.connect(self.refresh)
        self.refresh()
        later.setDefault(True)

    def refresh(self):
        self.approve.setEnabled('approve' not in self.snapshot['errors'] and all(c.isChecked() for c in self.checks))
        self.changes.setEnabled('changes' not in self.snapshot['errors'] and bool(self.feedback.toPlainText().strip()))

    def choose(self, decision):
        self.decision=decision
        self.accept()

    def selection(self):
        return self.decision, self.feedback.toPlainText().strip(), tuple(c.property('criterion') for c in self.checks if c.isChecked())
