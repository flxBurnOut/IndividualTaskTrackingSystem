"""Small native charts with explicit denominators and separate unknown coverage."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .gui_theme import color, LIGHT
from .review_display import KIND_LABELS, RESULT_LABELS, label as review_label, color_role

COLORS = {key: LIGHT[key] for key in ('done','incomplete','unreported','other_reported')}
LABELS = {'done': '完成', 'incomplete': '未完成', 'unreported': '未反馈', 'other_reported': '已反馈 · 状态待核对'}
ORIGINAL_LABELS = {'attended':'已参加','absent':'未参加','missed_needs_catchup':'缺课需补','partial': '部分完成', 'not_started': '未开始', 'blocked': '受阻', 'cancelled': '已取消', 'unknown': '待确认', 'done': '已完成', 'incomplete': '未完成'}


def count(value):
    return value if type(value) is int and value >= 0 else 0


def coverage(summary):
    values = {key: count(summary.get(key)) for key in LABELS}
    total = count(summary.get('total', summary.get('planned')))
    # Any residual is unclassified coverage, never quietly reclassified as failure.
    total = max(total, sum(values.values()))
    return total, values


def display_segments(summary):
    total,values=coverage(summary)
    if 'breakdown' in summary:
        rows=[dict(row) for row in summary['breakdown'] if count(row.get('count'))]
    else:
        rows=[]
        for key in ('done','incomplete'):
            if values[key]:rows.append({'key':key,'kind':'task','result':key,'count':values[key]})
        remaining=values['other_reported']
        for raw,n in (summary.get('original_results') or {}).items():
            n=min(remaining,count(n))
            if not n:continue
            kind='attendance' if raw in {'attended','absent','missed_needs_catchup'} else 'task'
            rows.append({'key':'legacy:'+str(raw),'kind':kind,'result':raw,'count':n});remaining-=n
        if remaining:rows.append({'key':'other_reported','kind':'task','result':'reported','count':remaining})
        if values['unreported']:rows.append({'key':'unreported','kind':'task','result':'unreported','count':values['unreported']})
    for row in rows:
        row['label']=row.get('label') or review_label(row);row['color']=color_role(row)
    return rows


class CoverageBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.total, self.values = 0, {key: 0 for key in LABELS}
        self.segments=[]
        self.setMinimumHeight(18)
        self.setMaximumHeight(22)
        self.setMinimumWidth(100)

    def set_summary(self, summary):
        self.total, self.values = coverage(summary)
        self.segments=display_segments(summary)
        self.setAccessibleName('；'.join(f"{row['label']} {row['count']} 项" for row in self.segments))
        self.setToolTip(self.accessibleName())
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
            for row in self.segments:
                width = self.width() * row['count'] / self.total
                if width:
                    painter.fillRect(QRectF(left, 2, width, rect.height()), QColor(color(row['color'])))
                    left += width
        painter.end()


class CoverageChart(QWidget):
    """Count-based coverage, explicitly not work-volume or mastery progress."""
    geometry_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = {}
        self.chart_style = 'bar'
        self._updating_geometry = False
        layout = QVBoxLayout(self)
        self._content_layout = layout
        # Height is recomputed below using wrapped text at the actual width.
        # SetMinimumSize would replace that value with a width-free minimum.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(0, 0, 0, 0)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setObjectName('ReviewSummary')
        layout.addWidget(self.heading)
        self.bar = CoverageBar()
        layout.addWidget(self.bar)
        self.details_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        layout.addLayout(self.details_row)
        self.ring = CoverageRing()
        self.ring.geometry_changed.connect(self._reflow)
        self.details_row.addWidget(self.ring)
        self.ring.hide()
        details = QVBoxLayout()
        self._details_layout = details
        self.details_row.addLayout(details, 1)
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        details.addWidget(self.legend)
        self.denominator = QLabel()
        self.denominator.setWordWrap(True)
        self.denominator.setObjectName('Hint')
        details.addWidget(self.denominator)
        self.originals = QLabel()
        self.originals.setWordWrap(True)
        self.originals.setObjectName('Hint')
        details.addWidget(self.originals)
        self.set_summary({})

    def set_style(self, style):
        if style not in {'bar', 'ring'}:
            raise ValueError('Coverage chart style must be bar or ring')
        self.chart_style = style
        self.bar.setVisible(style == 'bar')
        self.ring.setVisible(style == 'ring')
        self._reflow()

    def _reflow(self):
        if not hasattr(self, 'originals') or self._updating_geometry:
            return
        self._updating_geometry = True
        try:
            horizontal = self.width() >= max(380, self.fontMetrics().height() * 19)
            self.details_row.setDirection(QBoxLayout.Direction.LeftToRight if horizontal else QBoxLayout.Direction.TopToBottom)
            self.ring.setMaximumWidth(self.ring.height() if horizontal else 16777215)
            self._details_layout.invalidate()
            self.details_row.invalidate()
            self._content_layout.invalidate()
            # A word-wrapped legend needs its height at the actual card width.
            # The bar's former size hint must not compress a newly visible ring.
            required = max(self._content_layout.minimumSize().height(),
                           self._content_layout.totalHeightForWidth(max(1, self.width())))
            changed = required != self.minimumHeight()
            self.setMinimumHeight(required)
            self.updateGeometry()
            self._content_layout.activate()
        finally:
            self._updating_geometry = False
        if changed:
            self.geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def heightForWidth(self, width):
        # Parent grids use this value instead of minimumSizeHint for wrapped
        # content. Never advertise a height below the active chart's minimum.
        if not hasattr(self, '_content_layout'):
            return super().heightForWidth(width)
        return max(self.minimumHeight(), self._content_layout.totalHeightForWidth(max(1, width)))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange}:
            self._reflow()

    def set_summary(self, summary, *, denominator='当日计划项目'):
        self.summary = dict(summary)
        total, values = coverage(summary)
        self.bar.set_summary(summary)
        self.ring.set_summary(summary)
        self.heading.setText(f'已完成 {values["done"]} / {total} 项' if total else '没有可统计的计划项目')
        if summary.get('fixed_scheduled'):
            reported=total-values['unreported']
            self.heading.setText(f"已反馈 {reported} / {total} 项 · 任务完成与课程出勤分别统计")
        self.legend.setText('    '.join(f"{row['label']} {row['count']}" for row in display_segments(summary)))
        self.denominator.setText(f'分母：{denominator}，共 {total} 项。按项目计数，不代表工作量、工时或掌握程度。' if total else '没有计划项目不表示失败，也不按 0% 完成处理。')
        originals = summary.get('original_results') or {}
        text = '原有反馈：' + '；'.join(f'{ORIGINAL_LABELS.get(str(k), str(k))} {count(v)} 项' for k, v in originals.items() if count(v))
        self.originals.setText(text if text != '原有反馈：' else '')
        self.originals.setVisible(bool(self.originals.text()) and 'breakdown' not in summary)
        self._reflow()


class CoverageRing(QWidget):
    """The same evidence segments as the bar; the centre shows count, never a grade."""
    geometry_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.total, self.values, self.segments = 0, {}, []
        self.summary = {}
        self.empty_label = '暂无项目'
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._fit_font()

    def _fit_font(self):
        height=max(124, self.fontMetrics().height() * 6)
        width=max(110, self.fontMetrics().horizontalAdvance('暂无项目') + 52)
        changed=height!=self.minimumHeight() or width!=self.minimumWidth()
        self.setFixedHeight(height)
        self.setMinimumWidth(width)
        if changed:self.geometry_changed.emit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange}:
            self._fit_font()

    def set_summary(self, summary, *, empty_label='暂无项目'):
        self.summary = dict(summary)
        self.total, self.values = coverage(summary)
        self.segments = display_segments(summary)
        self.empty_label = empty_label
        description = '；'.join(f"{row['label']} {row['count']} 项" for row in self.segments)
        self.setAccessibleName((f'分母 {self.total} 项；' + description) if self.total else empty_label + '；不计算完成占比。')
        self.setToolTip(self.accessibleName())
        self.update()

    def segment_angles(self):
        if not self.total:
            return []
        return [(row, 360 * row['count'] / self.total) for row in self.segments]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = max(1, min(self.width(), self.height()) - 16)
        thickness = max(10, diameter * .15)
        rect = QRectF((self.width() - diameter) / 2 + thickness / 2,
                      (self.height() - diameter) / 2 + thickness / 2,
                      diameter - thickness, diameter - thickness)
        painter.setPen(QPen(QColor(color('chart_track')), thickness))
        painter.drawEllipse(rect)
        position = 90.0
        for row, angle in self.segment_angles():
            painter.setPen(QPen(QColor(color(row['color'])), thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            painter.drawArc(rect, round(position * 16), -round(angle * 16))
            position -= angle
        painter.setPen(QColor(color('chart_label')))
        text = f'{self.total}\n项' if self.total else '暂无\n项目' if self.empty_label == '暂无项目' else self.empty_label
        painter.drawText(rect.adjusted(thickness, thickness, -thickness, -thickness), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
        painter.end()


class VerticalCoverageBar(QWidget):
    """Each nonempty day fills the same 100% height, regardless of item count."""
    def __init__(self, day, parent=None):
        super().__init__(parent)
        self.day = dict(day)
        self.total, self.values = coverage(day.get('summary') or {})
        self.segments=display_segments(day.get('summary') or {})
        self.kind = 'missing_plan' if not day.get('can_review',day.get('has_plan')) else 'empty_plan' if not self.total else 'data'
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
            f"{row['label']}：{row['count']} 项（{row['count'] / self.total:.0%}）" for row in self.segments) + '\n按项目计数，不代表工作量或掌握程度。'

    def plot_rect(self):
        width = max(16, min(88, self.width() - 12))
        return QRectF((self.width() - width) / 2, 8, width, self.height() - 16)

    def segment_rects(self):
        if self.kind != 'data':
            return []
        rect = self.plot_rect()
        bottom = rect.bottom()
        result = []
        for row in self.segments:
            height = rect.height() * row['count'] / self.total
            bottom -= height
            result.append((row['key'], QRectF(rect.left(), bottom, rect.width(), height), row['count'] / self.total))
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
                row=next(row for row in self.segments if row['key']==key)
                painter.fillRect(segment, QColor(color(row['color'])))
                painter.setPen(QColor(color('chart_on_unknown' if row['color']=='unreported' else 'chart_on_color')))
                percent = f'{ratio:.0%}'
                lines=[KIND_LABELS.get(row['kind'],'事项'),RESULT_LABELS.get(str(row['result']),str(row['result'])),percent]
                fits=segment.height()>=self.fontMetrics().height()*len(lines)+4 and segment.width()>=max(self.fontMetrics().horizontalAdvance(line) for line in lines)+4
                text='\n'.join(lines) if fits else percent
                if segment.height() >= self.fontMetrics().height() + 2 and segment.width() >= self.fontMetrics().horizontalAdvance(percent) + 2:
                    painter.drawText(segment.adjusted(1, 1, -1, -1), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class WeekDaysChart(QWidget):
    date_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days, self.day_labels, self.bars = [], [], []
        self.cards = []
        self.cards_grid = None
        self.layout_style = 'columns'
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

    def set_style(self, style):
        if style not in {'columns', 'rows', 'tiles'}:
            raise ValueError('Weekly chart style must be columns, rows or tiles')
        if self.layout_style != style:
            self.layout_style = style
            self._render()

    def set_days(self, days):
        self.days = [dict(day) for day in days]
        self._render()

    @staticmethod
    def _detail(day):
        total, values = coverage(day.get('summary') or {})
        if not day.get('can_review',day.get('has_plan')):
            return '缺计划 · 请按实际情况复盘'
        if not total:
            return '空计划 · 当日没有计划项目'
        return '；'.join(f"{row['label']} {row['count']}" for row in display_segments(day.get('summary') or {}))

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
                item.widget().hide()
                item.widget().deleteLater()
        self.day_labels, self.bars, self.cards = [], [], []
        self.cards_grid = None
        hints = {
            'columns': '每天同高为 100%；按事项类别和实际结果分段，课程出勤与任务完成分别标明。未反馈保持未知。',
            'rows': '每行按事项类别和实际结果展示反馈；没有计划项目的日期不计算占比。',
            'tiles': '每张卡片表示一天。圆环按当日事项计数，中央是分母；无计划、空计划与未反馈分别保留，不把未知算作失败。',
        }
        hint = QLabel(hints[self.layout_style])
        hint.setWordWrap(True)
        hint.setObjectName('Hint')
        self.layout.addWidget(hint)
        if self.layout_style == 'columns':
            self._columns()
        elif self.layout_style == 'rows':
            self._rows()
        else:
            self._tiles()
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
            if bar.kind=='data':
                text+='\n'+'\n'.join(f"{entry['label']} {entry['count']}" for entry in bar.segments)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(self._detail(day))
            label.setObjectName('Hint')
            layout.addWidget(label)
            layout.addStretch()
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

    def _tiles(self):
        container = QWidget()
        self.cards_grid = QGridLayout(container)
        self.cards_grid.setContentsMargins(0, 6, 0, 6)
        self.cards_grid.setSpacing(10)
        for day in self.days:
            card = QFrame()
            card.setObjectName('PlanCard')
            layout = QVBoxLayout(card)
            layout.setContentsMargins(12, 10, 12, 10)
            date = str(day.get('date', ''))
            layout.addWidget(self._date_button(date))
            total, _ = coverage(day.get('summary') or {})
            has_plan = day.get('can_review', day.get('has_plan'))
            ring = CoverageRing()
            ring.set_summary(day.get('summary') or {}, empty_label='无计划' if not has_plan else '空计划')
            layout.addWidget(ring)
            label = QLabel(self._detail(day))
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(ring.accessibleName())
            layout.addWidget(label)
            denominator = QLabel(f'分母：当日 {total} 项' if total else '没有计划分母，不计算占比')
            denominator.setWordWrap(True)
            denominator.setObjectName('Hint')
            denominator.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(denominator)
            layout.addStretch()
            self.cards.append(card)
            self.bars.append(ring)
            self.day_labels.append(label)
        self.layout.addWidget(container)
        self._reflow_tiles()

    def _reflow_tiles(self):
        if getattr(self, 'cards_grid', None) is None:
            return
        minimum = max(175, self.fontMetrics().horizontalAdvance('已反馈 · 状态待核对') + 35)
        spacing = self.cards_grid.horizontalSpacing()
        columns = max(1, min(4, (self.width() + spacing) // (minimum + spacing)))
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for index, card in enumerate(self.cards):
            self.cards_grid.addWidget(card, index // columns, index % columns)
        for index in range(4):
            self.cards_grid.setColumnStretch(index, 1 if index < columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_tiles()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange}:
            self._reflow_tiles()
