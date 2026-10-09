"""Small original outline symbols; no platform-specific icon-font dependency."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_PATHS = {
    'dashboard': '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    'today': '<rect x="3" y="5" width="18" height="16" rx="4"/><path d="M7 3v4m10-4v4M3 10h18"/><circle cx="12" cy="15" r="2"/>',
    'tasks': '<rect x="3" y="4" width="18" height="16" rx="4"/><path d="m6 9 1.5 1.5L10 8m-4 7 1.5 1.5L10 14m3-5h5m-5 6h5"/>',
    'projects': '<path d="M3 7a3 3 0 0 1 3-3h4l2 3h6a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3Z"/>',
    'reviews': '<path d="M4 20h16M6 16v-4m6 4V5m6 11V9"/>',
    'habits': '<path d="M4 6h16M4 12h16M4 18h16"/><rect x="7" y="3" width="4" height="6" rx="2"/><rect x="14" y="9" width="4" height="6" rx="2"/><rect x="8" y="15" width="4" height="6" rx="2"/>',
    'assistant': '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6Z"/>',
    'help': '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2l-1.5 1.5V14m0 3h.01"/>',
    'settings': '<circle cx="12" cy="12" r="3.2"/><path d="m9 3-.6 2.2-2.2 1L4 5.5 2.5 8l1.6 1.6v2.5L2.5 14 4 16.5l2.2-.5 2.2 1L9 20h3l.6-3 2.2-1 2.2.5 1.5-2.5-1.6-1.9V9.6L18.5 8 17 5.5l-2.2.7-2.2-1L12 3Z" transform="translate(1 0)"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    'update': '<path d="M20 10a8 8 0 1 0-1 7M20 4v6h-6"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
}


def symbol_icon(name, tint, size=20):
    """Render at 2x so glyphs remain crisp at fractional Windows scaling."""
    content = _PATHS.get(name, _PATHS['dashboard'])
    icon = QIcon()
    for mode, alpha in ((QIcon.Mode.Normal, 1.0), (QIcon.Mode.Disabled, .38)):
        color = QColor(tint)
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g fill="none" stroke="{color.name()}" opacity="{alpha}" stroke-width="1.65" '
               'stroke-linecap="round" stroke-linejoin="round">' + content + '</g></svg>')
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(svg.encode())).render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(2)
        icon.addPixmap(pixmap, mode)
    return icon
