"""Shared native month popup; styling never changes the editor's date semantics."""
from PySide6.QtCore import QDate, QLocale, Qt, QPoint, QSize
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QIcon, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import QCalendarWidget, QDateEdit, QToolButton

from .gui_theme import bind_theme, color, current_appearance


def refresh_calendar(calendar):
    header = QTextCharFormat()
    header.setForeground(QColor(color('muted')))
    header.setBackground(QColor(color('calendar_header')))
    calendar.setHeaderTextFormat(header)
    weekend = QTextCharFormat()
    weekend.setForeground(QColor(color('muted')))
    calendar.setWeekdayTextFormat(Qt.DayOfWeek.Saturday, weekend)
    calendar.setWeekdayTextFormat(Qt.DayOfWeek.Sunday, weekend)
    today = QTextCharFormat()
    today.setFontWeight(QFont.Weight.Bold)
    today.setForeground(QColor(color('selection_text')))
    today.setBackground(QColor(color('calendar_today')))
    calendar.setDateTextFormat(QDate.currentDate(), today)
    scale = current_appearance()['font_size'] / 13
    calendar.setMinimumSize(round(336 * scale), round(290 * scale))
    # Calendar's built-in pixmap arrows remain black under a dark QSS. Draw small
    # palette-aware native icons while retaining Qt's navigation and semantics.
    for name, points in [('qt_calendar_prevmonth', [(12,3),(5,10),(12,17)]), ('qt_calendar_nextmonth', [(7,3),(14,10),(7,17)])]:
        button = calendar.findChild(QToolButton, name)
        if button is not None:
            pixmap = QPixmap(20,20); pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(color('text')))
            painter.drawPolygon(QPolygon([QPoint(x,y) for x,y in points])); painter.end()
            button.setIcon(QIcon(pixmap)); button.setIconSize(QSize(round(16*scale),round(16*scale)))


class ManagementCalendar(QCalendarWidget):
    def refresh_theme(self):
        refresh_calendar(self)


def install_calendar(date_edit):
    """Install once and return the calendar; preserve selected/min/max dates."""
    if not isinstance(date_edit, QDateEdit):
        raise TypeError('install_calendar expects QDateEdit')
    existing = getattr(date_edit, '_management_calendar', None)
    if existing is not None:
        return existing
    selected, minimum, maximum = date_edit.date(), date_edit.minimumDate(), date_edit.maximumDate()
    calendar = ManagementCalendar(date_edit)
    calendar.setObjectName('ManagementCalendar')
    calendar.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
    calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
    calendar.setGridVisible(False)
    bind_theme(calendar, calendar.refresh_theme)
    calendar.setDateRange(minimum, maximum)
    calendar.setSelectedDate(selected)
    calendar.setAccessibleName('选择日期，按月浏览')
    date_edit.setCalendarPopup(True)
    date_edit.setCalendarWidget(calendar)
    date_edit.setDate(selected)
    date_edit._management_calendar = calendar
    return calendar
