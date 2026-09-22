"""Small native charts with explicit denominators and separate unknown coverage."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .gui_theme import color, LIGHT

COLORS = {key: LIGHT[key] for key in ('done','incomplete','unreported','other_reported')}
LABELS = {'done': '完成', 'incomplete': '未完成', 'unreported': '未反馈', 'other_reported': '其他已反馈'}
ORIGINAL_LABELS = {'partial': '部分完成', 'not_started': '未开始', 'blocked': '受阻', 'cancelled': '已取消', 'unknown': '待确认', 'done': '已完成', 'incomplete': '未完成'}


def count(value):
    return value if type(value) is int and value >= 0 else 0


def coverage(summary):
    values = {key: count(summary.get(key)) for key in LABELS}
    total = count(summary.get('total', summary.get('planned')))
    # Any residual is unclassified coverage, never quietly reclassified as failure.
    total = max(total, sum(values.values()))
    return total, values


class CoverageBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.total, self.values = 0, {key: 0 for key in LABELS}
        self.setMinimumHeight(18)
        self.setMaximumHeight(22)
        self.setMinimumWidth(100)

    def set_summary(self, summary):
        self.total, self.values = coverage(summary)
        self.setAccessibleName('；'.join(f'{LABELS[k]} {v} 项' for k, v in self.values.items()))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 2, self.width(), max(1, self.height() - 4))
        path = QPainterPath()
        path.addRoundedRect(rect, 5, 5)
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(color('chart_track')))
        if self.total:
            left = 0.0
            for key in ('done', 'incomplete', 'other_reported', 'unreported'):
                width = self.width() * self.values[key] / self.total
                if width:
                    painter.fillRect(QRectF(left, 2, width, rect.height()), QColor(color(key)))
                    left += width
        painter.end()


class CoverageChart(QWidget):
    """Count-based coverage, explicitly not work-volume or mastery progress."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setObjectName('ReviewSummary')
        layout.addWidget(self.heading)
        self.bar = CoverageBar()
        layout.addWidget(self.bar)
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        self.denominator = QLabel()
        self.denominator.setWordWrap(True)
        self.denominator.setObjectName('Hint')
        layout.addWidget(self.denominator)
        self.originals = QLabel()
        self.originals.setWordWrap(True)
        self.originals.setObjectName('Hint')
        layout.addWidget(self.originals)
        self.set_summary({})

    def set_summary(self, summary, *, denominator='当日计划项目'):
        self.summary = dict(summary)
        total, values = coverage(summary)
        self.bar.set_summary(summary)
        self.heading.setText(f'已完成 {values["done"]} / {total} 项' if total else '没有可统计的计划项目')
        self.legend.setText('    '.join(f'{LABELS[key]} {values[key]}' for key in ('done', 'incomplete', 'unreported', 'other_reported') if values[key] or key != 'other_reported'))
        self.denominator.setText(f'分母：{denominator}，共 {total} 项。按项目计数，不代表工作量、工时或掌握程度。' if total else '没有计划项目不表示失败，也不按 0% 完成处理。')
        originals = summary.get('original_results') or {}
        text = '原有反馈：' + '；'.join(f'{ORIGINAL_LABELS.get(str(k), str(k))} {count(v)} 项' for k, v in originals.items() if count(v))
        self.originals.setText(text if text != '原有反馈：' else '')
        self.originals.setVisible(bool(self.originals.text()))


class VerticalCoverageBar(QWidget):
    """Each nonempty day fills the same 100% height, regardless of item count."""
    def __init__(self, day, parent=None):
        super().__init__(parent)
        self.day = dict(day)
        self.total, self.values = coverage(day.get('summary') or {})
        self.kind = 'missing_plan' if not day.get('has_plan') else 'empty_plan' if not self.total else 'data'
        self.setFixedHeight(228)
        self.setMinimumWidth(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(self.description())
        self.setToolTip(self.description())

    def description(self):
        if self.kind == 'missing_plan':
            return str(self.day.get('date', '')) + '：缺计划；没有计划分母，不计算完成占比。'
        if self.kind == 'empty_plan':
            return str(self.day.get('date', '')) + '：空计划；没有计划项目，不计算完成占比。'
        return str(self.day.get('date', '')) + f'：分母为当日 {self.total} 个计划项目。\n' + '\n'.join(
            f'{LABELS[key]}：{value} 项（{value / self.total:.0%}）' for key, value in self.values.items()) + '\n按项目计数，不代表工作量或掌握程度。'

    def plot_rect(self):
        width = max(16, min(88, self.width() - 12))
        return QRectF((self.width() - width) / 2, 8, width, self.height() - 16)

    def segment_rects(self):
        if self.kind != 'data':
            return []
        rect = self.plot_rect()
        bottom = rect.bottom()
        result = []
        for key in ('done', 'incomplete', 'other_reported', 'unreported'):
            if self.values[key]:
                height = rect.height() * self.values[key] / self.total
                bottom -= height
                result.append((key, QRectF(rect.left(), bottom, rect.width(), height), self.values[key] / self.total))
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.plot_rect()
        path = QPainterPath()
        path.addRoundedRect(rect, 7, 7)
        if self.kind != 'data':
            painter.setPen(QPen(QColor(color('chart_grid')), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(color('surface_alt')))
            painter.drawRoundedRect(rect, 7, 7)
            painter.setPen(QColor(color('chart_label')))
            text = '缺计划\n无占比' if self.kind == 'missing_plan' else '空计划\n无项目'
            painter.drawText(rect.adjusted(2, 4, -2, -4), Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.setClipPath(path)
            painter.fillRect(rect, QColor(color('chart_track')))
            for key, segment, ratio in self.segment_rects():
                painter.fillRect(segment, QColor(color(key)))
                painter.setPen(QColor(color('chart_on_unknown' if key == 'unreported' else 'chart_on_color')))
                percent = f'{ratio:.0%}'
                label = {'done':'完成','incomplete':'未完成','other_reported':'其他','unreported':'未反馈'}[key]
                text = label + '\n' + percent if segment.height() >= self.fontMetrics().height() * 2 + 4 and segment.width() >= self.fontMetrics().horizontalAdvance(label) + 4 else percent
                if segment.height() >= self.fontMetrics().height() + 2 and segment.width() >= self.fontMetrics().horizontalAdvance(percent) + 2:
                    painter.drawText(segment.adjusted(1, 1, -1, -1), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class WeekDaysChart(QWidget):
    date_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days, self.day_labels, self.bars = [], [], []
        self.layout_style = 'columns'
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

    def set_style(self, style):
        if style not in {'columns', 'rows'}:
            raise ValueError('Weekly chart style must be columns or rows')
        if self.layout_style != style:
            self.layout_style = style
            self._render()

    def set_days(self, days):
        self.days = [dict(day) for day in days]
        self._render()

    @staticmethod
    def _detail(day):
        total, values = coverage(day.get('summary') or {})
        if not day.get('has_plan'):
            return '缺计划 · 请按实际情况复盘'
        if not total:
            return '空计划 · 当日没有计划项目'
        text = f'完成 {values["done"]} · 未完成 {values["incomplete"]} · 未反馈 {values["unreported"]}'
        if values['other_reported']:
            text += f' · 其他已反馈 {values["other_reported"]}'
        return text

    def _date_button(self, date):
        button = QPushButton(date[5:] if len(date) == 10 else date)
        button.setStyleSheet('QPushButton { padding: 6px 3px; }')
        button.setToolTip('查看 ' + date + ' 的每日复盘')
        button.setAccessibleName('查看 ' + date + ' 的每日复盘')
        button.clicked.connect(lambda checked=False, value=date: self.date_selected.emit(value))
        return button

    def _render(self):
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.day_labels, self.bars = [], []
        hint = QLabel('每天同高为 100%；柱内按当日计划项目数分段，缺计划和空计划不计算占比。' if self.layout_style == 'columns'
                      else '每行按当日计划项目数展示反馈覆盖；缺计划和空计划不计算占比。')
        hint.setWordWrap(True)
        hint.setObjectName('Hint')
        self.layout.addWidget(hint)
        if self.layout_style == 'columns':
            self._columns()
        else:
            self._rows()
        self.layout.addStretch()

    def _columns(self):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 10, 0, 4)
        row.setSpacing(5)
        import datetime
        for day in self.days:
            column = QWidget()
            column.setMinimumWidth(44)
            layout = QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            date = str(day.get('date', ''))
            try:
                weekday = '周' + '一二三四五六日'[datetime.date.fromisoformat(date).weekday()]
            except ValueError:
                weekday = ''
            title = QLabel(weekday)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setObjectName('Hint')
            layout.addWidget(title)
            bar = VerticalCoverageBar(day)
            layout.addWidget(bar)
            layout.addWidget(self._date_button(date))
            text = '缺计划' if bar.kind == 'missing_plan' else '空计划' if bar.kind == 'empty_plan' else f'{bar.total} 项'
            label = QLabel(text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(self._detail(day))
            label.setObjectName('Hint')
            layout.addWidget(label)
            row.addWidget(column, 1)
            self.day_labels.append(label)
            self.bars.append(bar)
        self.layout.addWidget(container)

    def _rows(self):
        for day in self.days:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 6, 0, 6)
            date = str(day.get('date', ''))
            button = self._date_button(date)
            button.setFixedWidth(65)
            line.addWidget(button)
            bar = CoverageBar()
            bar.set_summary(day.get('summary') or {})
            line.addWidget(bar, 1)
            label = QLabel(self._detail(day))
            label.setWordWrap(True)
            label.setMinimumWidth(180)
            line.addWidget(label)
            self.layout.addWidget(row)
            self.day_labels.append(label)
            self.bars.append(bar)
