"""One palette and stylesheet for all native widgets, drawings and open dialogs."""
from __future__ import annotations
import weakref
from PySide6.QtCore import QThread
from shiboken6 import isValid
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication
from .appearance import DEFAULT_APPEARANCE, normalize_appearance
from .gui_theme_glass import DARK, LIGHT, palette_tokens, stylesheet as glass_stylesheet


def current_appearance():
    app = QApplication.instance()
    return dict(getattr(app, '_management_appearance', DEFAULT_APPEARANCE)) if app else dict(DEFAULT_APPEARANCE)


def color(name):
    config = current_appearance()
    return palette_tokens(config['theme'], config.get('accent', 'default'))[name]


def available_font_families():
    return sorted(set(QFontDatabase.families()), key=str.casefold)


def resolved_font_family(config):
    family = config['font_family']
    families = available_font_families()
    if family in families:
        return family
    if not family and 'Microsoft YaHei UI' in families:
        return 'Microsoft YaHei UI'
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def stylesheet(config=None):
    return glass_stylesheet(normalize_appearance(config))


def _bindings(app):
    result = []
    for owner, method in getattr(app, '_management_theme_bindings', []):
        widget = owner()
        if widget is not None and isValid(widget) and method() is not None:
            result.append((owner, method))
    app._management_theme_bindings = result
    return result


def bind_theme(widget, callback):
    """Keep no owning reference to a widget, signal slot, or anonymous closure.

    A bound method is required so its weak reference cannot retain a QWidget and
    later cause Python's cyclic collector to delete it on an I/O worker thread.
    """
    app = QApplication.instance()
    if app is None:
        return
    if QThread.currentThread() != app.thread():
        raise RuntimeError('Theme binding requires the GUI thread')
    method = weakref.WeakMethod(callback)
    _bindings(app).append((weakref.ref(widget), method))
    callback()


def apply_appearance(app, config=None):
    from .gui_gc import install_gui_gc
    install_gui_gc(app)
    if QThread.currentThread() != app.thread():
        raise RuntimeError('Applying appearance requires the GUI thread')
    from .gui_indicators import install_indicators
    install_indicators(app)
    config = normalize_appearance(config)
    old = getattr(app, '_management_appearance', None)
    # Sidebar persistence is not a palette change; avoid rebuilding all widgets.
    if old and all(old.get(k) == config[k] for k in ('theme','accent','font_family','font_size')):
        app._management_appearance = dict(config)
        return config
    app._management_appearance = dict(config)
    palette = QPalette()
    for role, name in {
        QPalette.ColorRole.Window: 'window', QPalette.ColorRole.WindowText: 'text',
        QPalette.ColorRole.Base: 'surface', QPalette.ColorRole.AlternateBase: 'surface_alt',
        QPalette.ColorRole.ToolTipBase: 'surface', QPalette.ColorRole.ToolTipText: 'text',
        QPalette.ColorRole.Text: 'text', QPalette.ColorRole.Button: 'surface',
        QPalette.ColorRole.ButtonText: 'text', QPalette.ColorRole.Highlight: 'selection',
        QPalette.ColorRole.HighlightedText: 'selection_text', QPalette.ColorRole.PlaceholderText: 'muted',
        QPalette.ColorRole.Link: 'primary', QPalette.ColorRole.LinkVisited: 'other_reported',
    }.items():
        palette.setColor(role, QColor(color(name)))
    for role in (QPalette.ColorRole.Text,QPalette.ColorRole.WindowText,QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color('disabled_text')))
    app.setPalette(palette)
    font = QFont(resolved_font_family(config))
    font.setPixelSize(config['font_size'])
    app.setFont(font)
    app.setStyleSheet(stylesheet({**config, 'font_family': font.family()}))
    for owner, method in list(_bindings(app)):
        widget, callback = owner(), method()
        if widget is not None and isValid(widget) and callback is not None:
            callback()
    for widget in app.allWidgets():
        widget.updateGeometry()
        widget.update()
    return config
