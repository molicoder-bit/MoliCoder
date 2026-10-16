"""Contiguous selection across the separate rich-text message cards."""
from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QTextCursor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QMenu
from shiboken6 import isValid


class TranscriptSelection(QObject):
    def __init__(self, scroll):
        super().__init__(scroll)
        self.scroll = scroll
        self._views = {}
        self._bubbles = []
        self._anchor = None
        self._dragging = False
        self._crossing = False
        self._pointer = None
        self._timer = QTimer(self)
        self._timer.setInterval(35)
        self._timer.timeout.connect(self._auto_scroll)

    def register_bubble(self, bubble):
        self._bubbles.append(bubble)
        for view in (bubble.reasoning_view, bubble.body):
            key = id(view)
            self._views[key] = view
            view.installEventFilter(self)
            view.viewport().installEventFilter(self)
            view.destroyed.connect(lambda _=None, key=key: self._views.pop(key, None))

    def reset(self):
        self.clear()
        for view in self._live_views():
            view.removeEventFilter(self)
            view.viewport().removeEventFilter(self)
        self._views.clear()
        self._bubbles.clear()

    def _live_views(self):
        for key, view in list(self._views.items()):
            if not isValid(view):
                self._views.pop(key, None)
        return list(self._views.values())

    def snapshot(self):
        return {
            'ranges': [(v.textCursor().anchor(), v.textCursor().position()) for v in self._live_views()],
            'expanded': [bubble.reasoning_toggle.isChecked() for bubble in self._bubbles if isValid(bubble)],
        }

    def restore(self, snapshot):
        for bubble, expanded in zip(self._bubbles, snapshot['expanded']):
            bubble.reasoning_toggle.setChecked(expanded)
        for view, (anchor, position) in zip(self._live_views(), snapshot['ranges']):
            if anchor != position:
                last = view.document().characterCount() - 1
                cursor = view.textCursor()
                cursor.setPosition(min(anchor, last))
                cursor.setPosition(min(position, last), QTextCursor.KeepAnchor)
                view.setTextCursor(cursor)
                if self._anchor is None:
                    self._anchor = (view, cursor.selectionStart())

    def _ordered(self):
        return sorted(
            (v for v in self._live_views() if v.isVisibleTo(self.scroll.widget())),
            key=lambda v: v.viewport().mapToGlobal(v.viewport().rect().topLeft()).y(),
        )

    def has_selection(self):
        return any(v.textCursor().hasSelection() for v in self._live_views())

    def clear(self):
        self._timer.stop()
        self._anchor = None
        self._dragging = self._crossing = False
        for view in self._live_views():
            cursor = view.textCursor()
            cursor.clearSelection()
            view.setTextCursor(cursor)

    def selected_text(self):
        return '\n\n'.join(
            view.textCursor().selectedText().replace('\u2029', '\n').replace('\u2028', '\n')
            for view in self._ordered() if view.textCursor().hasSelection()
        )

    def copy_selection(self):
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def select_all(self):
        views = self._ordered()
        if views:
            self._anchor = (views[0], 0)
            self._select_to(views[-1], views[-1].document().characterCount() - 1)
            views[0].setFocus()

    def _select_to(self, end_view, end_position):
        views = self._ordered()
        if self._anchor is None or self._anchor[0] not in views or end_view not in views:
            return
        start_view, start_position = self._anchor
        start, end = views.index(start_view), views.index(end_view)
        if (start, start_position) > (end, end_position):
            start, end, start_position, end_position = end, start, end_position, start_position
        for index, view in enumerate(views):
            cursor = view.textCursor()
            cursor.clearSelection()
            if start <= index <= end:
                first = start_position if index == start else 0
                last = end_position if index == end else view.document().characterCount() - 1
                cursor.setPosition(first)
                cursor.setPosition(last, QTextCursor.KeepAnchor)
            view.setTextCursor(cursor)

    def _locate(self, global_point):
        views = self._ordered()
        if not views:
            return None
        def distance(view):
            rect = view.viewport().rect()
            point = view.viewport().mapFromGlobal(global_point)
            return max(0, rect.top() - point.y(), point.y() - rect.bottom())
        view = min(views, key=distance)
        point = view.viewport().mapFromGlobal(global_point)
        if point.y() < 0:
            position = 0
        elif point.y() > view.viewport().height():
            position = view.document().characterCount() - 1
        else:
            position = view.cursorForPosition(point).position()
        return view, position

    def _extend(self):
        target = self._locate(self._pointer)
        if target is None or self._anchor is None:
            return False
        self._crossing = self._crossing or target[0] is not self._anchor[0]
        if self._crossing:
            self._select_to(*target)
        return self._crossing

    def _auto_scroll(self):
        if not self._dragging or self._pointer is None:
            self._timer.stop()
            return
        viewport = self.scroll.viewport()
        point = viewport.mapFromGlobal(self._pointer)
        direction = -1 if point.y() < 16 else 1 if point.y() > viewport.height() - 16 else 0
        if direction:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.value() + direction * 22)
            target = self._locate(self._pointer)
            if target is not None:
                self._crossing = True
                self._select_to(*target)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in {QEvent.KeyPress, QEvent.ContextMenu, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease}:
            return False
        view = next((v for v in self._live_views() if watched is v or watched is v.viewport()), None)
        if view is None:
            return False
        if kind == QEvent.KeyPress:
            if event.matches(QKeySequence.SelectAll):
                self.select_all()
                return True
            if event.matches(QKeySequence.Copy) and self.has_selection():
                self.copy_selection()
                return True
            if event.key() == Qt.Key_Escape:
                self.clear()
                return True
        if kind == QEvent.ContextMenu:
            menu = QMenu(view)
            menu.addAction('Copy selection', self.copy_selection).setEnabled(self.has_selection())
            menu.addAction('Select all messages', self.select_all)
            link = view.anchorAt(view.viewport().mapFromGlobal(event.globalPos()))
            if link:
                menu.addSeparator()
                menu.addAction('Open link', lambda: QDesktopServices.openUrl(QUrl(link)))
                menu.addAction('Copy link address', lambda: QGuiApplication.clipboard().setText(link))
            menu.exec(event.globalPos())
            menu.deleteLater()
            return True
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            point = event.globalPosition().toPoint()
            position = view.cursorForPosition(view.viewport().mapFromGlobal(point)).position()
            if event.modifiers() & Qt.ShiftModifier and self._anchor is not None:
                self._select_to(view, position)
                view.setFocus()
                return True
            self.clear()
            self._anchor = (view, position)
            self._pointer = point
            self._dragging = True
            self._timer.start()
        elif kind == QEvent.MouseMove and self._dragging and event.buttons() & Qt.LeftButton:
            self._pointer = event.globalPosition().toPoint()
            return self._extend()
        elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            consumed = self._crossing
            self._dragging = False
            self._timer.stop()
            if consumed:
                self._pointer = event.globalPosition().toPoint()
                self._extend()
            return consumed
        return False
