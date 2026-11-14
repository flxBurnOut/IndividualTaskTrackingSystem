"""Nullable task properties without a separate enable checkbox."""
from __future__ import annotations

import json
import copy
from decimal import Decimal
from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QCheckBox, QComboBox,
    QSpinBox, QDoubleSpinBox, QDateEdit, QLineEdit, QTextEdit, QPushButton)
from .gui_calendar import install_calendar


class TaskFieldEditor(QWidget):
    changed = Signal()

    def __init__(self, name, definition, value=None, parent=None):
        super().__init__(parent)
        self.name = name
        self.definition = definition if isinstance(definition, dict) else {'type': definition}
        self.kind = self.definition.get('type', 'string')
        self.kind = {'int': 'integer', 'float': 'number', 'str': 'string',
                     'bool': 'boolean', 'multiline': 'text'}.get(self.kind, self.kind)
        layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        # Old extensions may address this property. It is no longer a UI gate.
        self.enabled = QCheckBox(self); self.enabled.hide(); self.enabled.setChecked(True)
        self.enabled.toggled.connect(lambda active: self.clear_value() if not active else None)
        options = self.definition.get('enum') or self.definition.get('choices') or self.definition.get('options')
        if self.kind == 'boolean' or options:
            self.editor = QComboBox(); self.editor.addItem('未确定' if self.kind == 'boolean' else '未选择', None)
            if self.kind == 'boolean':
                self.editor.addItem('是', True); self.editor.addItem('否', False)
            else:
                for option in options:
                    self.editor.addItem(option.get('label', str(option.get('value'))) if isinstance(option, dict) else str(option),
                                        option.get('value') if isinstance(option, dict) else option)
            index = self.editor.findData(value)
            if value is not None and index < 0:
                self.editor.addItem(str(value), value); index = self.editor.count() - 1
            self.editor.setCurrentIndex(max(0, index)); self.editor.currentIndexChanged.connect(self.changed)
        elif self.kind in {'integer', 'number'}:
            self.editor = QSpinBox() if self.kind == 'integer' else QDoubleSpinBox()
            minimum = self.definition.get('minimum', -100000000)
            if self.kind == 'number':
                precision = max(2, -Decimal(str(value)).as_tuple().exponent) if value is not None else 6
                self.editor.setDecimals(min(12, precision))
            self._empty_number = minimum - 1
            self.editor.setRange(self._empty_number, self.definition.get('maximum', 100000000))
            self.editor.setSpecialValueText('未估计' if name == 'estimated_minutes' else '未填写')
            self.editor.setValue(value if value is not None else self._empty_number)
            self.editor.valueChanged.connect(self.changed)
            self.editor.lineEdit().textChanged.connect(self.changed)
        elif self.kind == 'date' or self.definition.get('format') == 'date':
            self.editor = QDateEdit(); install_calendar(self.editor, nullable=True)
            self.editor.setDisplayFormat('yyyy-MM-dd'); self.editor.setSpecialValueText('未设置')
            self.editor.setDate(QDate.fromString(str(value), 'yyyy-MM-dd') if value else self.editor.minimumDate())
            self.editor.dateChanged.connect(self.changed)
        elif self.kind in {'text', 'object', 'array'} or name in {'completion_gate', 'notes', 'source_text'}:
            self.editor = QTextEdit(); self.editor.setMaximumHeight(100)
            self.editor.setPlainText(json.dumps(value, ensure_ascii=False, indent=2) if value is not None and self.kind in {'object', 'array'} else str(value or ''))
            self.editor.textChanged.connect(self.changed)
        else:
            initial = '，'.join(str(item) for item in value) if self.kind == 'selection' and isinstance(value, list) else str(value) if value is not None else ''
            self.editor = QLineEdit(initial)
            self.editor.textChanged.connect(self.changed)
        layout.addWidget(self.editor, 1)
        self.clear_button = QPushButton('清除日期' if isinstance(self.editor, QDateEdit) else '清除')
        self.clear_button.clicked.connect(self.clear_value); layout.addWidget(self.clear_button)
        self.clear_button.setVisible(not self.definition.get('required', False))
        self._original_value = copy.deepcopy(value)
        self._initial_state = self._editor_state()
        self._cleared = False

    def clear_value(self):
        self._cleared = True
        widget = self.editor
        if isinstance(widget, QComboBox): widget.setCurrentIndex(0)
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)): widget.setValue(self._empty_number)
        elif isinstance(widget, QDateEdit): widget.setDate(widget.minimumDate())
        else: widget.clear()

    def _editor_state(self):
        widget = self.editor
        if isinstance(widget, QComboBox): return widget.currentIndex()
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)): return widget.lineEdit().text()
        if isinstance(widget, QDateEdit): return widget.date().toString('yyyy-MM-dd')
        return widget.toPlainText() if isinstance(widget, QTextEdit) else widget.text()

    def value(self):
        # Editing another task property must not round/coerce extension facts.
        if not self._cleared and self._original_value is not None and self._editor_state() == self._initial_state:
            return copy.deepcopy(self._original_value)
        widget = self.editor
        if isinstance(widget, QComboBox): value = widget.currentData()
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            value = None if not widget.lineEdit().text().strip() or widget.value() == self._empty_number else widget.value()
        elif isinstance(widget, QDateEdit):
            value = None if widget.date() == widget.minimumDate() else widget.date().toString('yyyy-MM-dd')
        else:
            text = (widget.toPlainText() if isinstance(widget, QTextEdit) else widget.text()).strip()
            value = json.loads(text) if text and self.kind in {'object', 'array'} else text or None
            if self.kind == 'selection' and text:
                value = list(dict.fromkeys(v.strip() for v in text.replace('，', ',').split(',') if v.strip()))
                if len(value) > 30: raise ValueError('一次最多填写 30 个类别。')
        if value is not None and self.kind in {'integer','number'}:
            if value < self.definition.get('minimum', -100000000) or value > self.definition.get('maximum', 100000000):
                raise ValueError('数值超出允许范围；未知时可以清除。')
        if value is None and self.definition.get('required'):
            raise ValueError('请填写' + self.definition.get('label', self.name) + '。')
        return value

    def display_value(self):
        try: value = self.value()
        except (ValueError, TypeError): return '待核对'
        if value is None: return '未估计' if self.name == 'estimated_minutes' else '未设置'
        return str(value) + (' 分钟' if self.name == 'estimated_minutes' else '')
