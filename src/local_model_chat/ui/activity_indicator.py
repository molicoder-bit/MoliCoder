"""A graphical activity indicator independent of provider status text."""
from PySide6.QtCore import QTimer, Qt, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget


class ActivityIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.advance)

    def advance(self):
        self.angle = (self.angle + 24) % 360
        self.update()

    def set_busy(self, busy, reduced_motion=False):
        self.setVisible(busy)
        if busy and not reduced_motion:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def paintEvent(self, event):
        from .theme import theme_color
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(theme_color('accent')), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(QRectF(4, 4, 18, 18), -self.angle * 16, 260 * 16)
