"""Compact, accessible controls used by the native settings pages."""
import math
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTextOption
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QTextBrowser, QSizePolicy

from .appearance import ACCENT_LABELS
from .gui_layout import ActionRow
from .gui_theme import bind_theme
from .gui_theme_glass import palette_tokens


def preview_tokens(theme, accent):
    return palette_tokens(theme, accent)


class SelectableText(QTextBrowser):
    """Plain text that can wrap inside a path without altering copied text."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet('QTextBrowser {background:transparent;border:0;padding:0;}')
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self.fit)
        self.setPlainText(text)
        bind_theme(self,self.refresh_theme)

    def text(self):
        return self.toPlainText()

    def setText(self,text):
        self.setPlainText(text)

    def sizeHint(self):
        return QSize(240,math.ceil(self.document().size().height())+2)

    def fit(self,*_):
        self.setFixedHeight(max(self.fontMetrics().height()+2,math.ceil(self.document().size().height())+2))

    def refresh_theme(self):
        self.document().setDefaultFont(self.font())
        self.fit()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.fit()


class SettingsGroup(QFrame):
    def __init__(self, title, description='', parent=None):
        super().__init__(parent)
        self.setObjectName('SettingsGroup')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 12, 2, 18)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName('SectionHeading')
        layout.addWidget(heading)
        if description:
            note = QLabel(description)
            note.setObjectName('Hint')
            note.setWordWrap(True)
            layout.addWidget(note)


class AccentPicker(QWidget):
    """Named color swatches with standard keyboard and exclusive button semantics."""
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = list(ACCENT_LABELS)
        self._index = 0
        self.buttons = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.row = ActionRow()
        layout.addWidget(self.row)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, key in enumerate(self._values):
            button = QPushButton(ACCENT_LABELS[key])
            button.setObjectName('AccentChoice')
            button.setCheckable(True)
            button.setAccessibleName('强调色：' + ACCENT_LABELS[key])
            button.setIconSize(QSize(18, 18))
            self.group.addButton(button, index)
            self.buttons.append(button)
            self.row.addWidget(button)
        self.buttons[0].setChecked(True)
        self.group.idClicked.connect(self.setCurrentIndex)
        self.set_theme('light')

    def currentData(self):
        return self._values[self._index]

    def findData(self, value):
        return self._values.index(value) if value in self._values else -1

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self._values):
            return
        self.buttons[index].setChecked(True)
        if self._index != index:
            self._index = index
            self.currentIndexChanged.emit(index)

    def set_theme(self, theme):
        for key, button in zip(self._values, self.buttons):
            pixels = QPixmap(36, 36)
            pixels.setDevicePixelRatio(2)
            pixels.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixels)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(preview_tokens(theme, key)['primary']))
            painter.drawEllipse(1, 1, 16, 16)
            painter.end()
            button.setIcon(QIcon(pixels))


class AppearancePreview(QFrame):
    """A local preview only: choosing a color never changes saved preferences."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('AppearancePreview')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        self.caption = QLabel('外观预览')
        self.caption.setObjectName('PreviewCaption')
        layout.addWidget(self.caption)
        title = QLabel('今天的安排')
        title.setObjectName('PreviewTitle')
        layout.addWidget(title)
        row = ActionRow()
        self.example = QPushButton('安排一件事')
        self.example.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.example.setAccessibleName('外观示例按钮，不执行操作')
        row.addWidget(self.example)
        note = QLabel('3 项待办 · 示例')
        note.setObjectName('PreviewCaption')
        row.addWidget(note)
        layout.addWidget(row)

    def set_appearance(self, value):
        tokens = preview_tokens(value['theme'], value['accent'])
        self.caption.setText(('深色' if value['theme'] == 'dark' else '浅色') + ' · ' + ACCENT_LABELS[value['accent']] + ' · 预览')
        self.setStyleSheet('''
            QFrame#AppearancePreview { background: %(window)s; border: 1px solid %(border)s; border-radius: 14px; }
            QFrame#AppearancePreview QLabel { color: %(text)s; background: transparent; border: 0; }
            QFrame#AppearancePreview QLabel#PreviewCaption { color: %(muted)s; }
            QFrame#AppearancePreview QLabel#PreviewTitle { font-weight: 600; }
            QFrame#AppearancePreview QPushButton { background: %(primary)s; color: %(primary_text)s; border: 0; border-radius: 9px; padding: 8px 14px; }
        ''' % tokens)
