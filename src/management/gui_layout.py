"""Natural-width actions that wrap through Qt's height-for-width layout."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QEvent, QTimer
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget, QLabel


class ReadingLabel(QLabel):
    """Keep wrapped text readable through nested layout size caches."""
    def __init__(self, text, name):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setObjectName(name)
        self._fit_queued = False

    def _fit_text(self):
        self._fit_queued = False
        area = self.contentsRect()
        if area.width() <= 0:
            return
        # Use the actual assigned width and style padding, without carrying
        # the previous minimum into the measurement. Rows can shrink again.
        bounds = self.fontMetrics().boundingRect(
            QRect(0, 0, area.width(), 100000),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            self.text())
        needed = max(0, bounds.height() + self.height() - area.height())
        if self.minimumHeight() != needed:
            self.setMinimumHeight(needed)

    def _queue_fit(self):
        if not getattr(self, '_fit_queued', False):
            self._fit_queued = True
            QTimer.singleShot(0, self, self._fit_text)

    def setText(self, text):
        super().setText(text)
        self._queue_fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_text()

    def showEvent(self, event):
        super().showEvent(event)
        self._queue_fit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange}:
            self._queue_fit()


class _FlowLayout(QLayout):
    def __init__(self, parent, spacing):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self):
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def sizeHint(self):
        sizes = [item.sizeHint() for item in self._items if not item.isEmpty()]
        left, top, right, bottom = self.getContentsMargins()
        return QSize(sum(size.width() for size in sizes) + max(0, len(sizes) - 1) * self.spacing()
                     + left + right, max((size.height() for size in sizes), default=0) + top + bottom)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def _arrange(self, rect, *, apply):
        left, top, right, bottom = self.getContentsMargins()
        available = max(0, rect.width() - left - right)
        lines, line, used, line_height = [], [], 0, 0
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint().expandedTo(item.minimumSize())
            width = min(available, size.width())
            height = item.heightForWidth(width) if item.hasHeightForWidth() else size.height()
            height = max(item.minimumSize().height(), height)
            space = self.spacing() if line else 0
            if line and used + space + width > available:
                lines.append((line, line_height))
                line, used, line_height, space = [], 0, 0, 0
            line.append((item, used + space, width, height))
            used += space + width
            line_height = max(line_height, height)
        if line:
            lines.append((line, line_height))
        y = rect.y() + top
        for index, (line, height) in enumerate(lines):
            if index:
                y += self.spacing()
            if apply:
                for item, x, width, item_height in line:
                    item.setGeometry(QRect(rect.x() + left + x, y + (height - item_height) // 2,
                                           width, item_height))
            y += height
        return y - rect.y() + bottom


class ActionRow(QWidget):
    """A compact row for existing controls; excess actions wrap onto new lines.

    No controls are rebuilt and no widths are cached, so font, visibility and
    text changes use their current Qt size hints. Labels and small widget groups
    are supported as well as buttons. A stretch is unnecessary in a flow row.
    """
    def __init__(self, parent=None, *, spacing=8):
        super().__init__(parent)
        self._flow = _FlowLayout(self, spacing)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def addWidget(self, widget):
        self._flow.addWidget(widget)

    def addStretch(self, *_):
        pass
