"""Release smoke harness for our own Qt window, without desktop capture."""
from pathlib import Path
import json
import time
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget
from PySide6.QtCore import QPoint, QRect, QTimer
from shiboken6 import isValid
from .gui import MainWindow
from .gui_theme import apply_appearance, current_appearance


def onboarding_step_geometry(overlay):
    """Independent screen-coordinate oracle for a real tutorial step.

    Do not use the overlay's rectangle resolver or target.mapTo(overlay): a
    shared coordinate bug would make the implementation validate itself.
    Only synthetic UI geometry and tutorial captions are included in reports.
    """
    def global_rect(widget):
        return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
    def serial(rect):
        return list(rect.getRect())
    step = overlay.steps[overlay.current_index]
    candidate = step.target() if callable(step.target) else step.target
    result = {'step': overlay.current_index + 1, 'title': step.title,
              'host_global': serial(global_rect(overlay.host)),
              'overlay_global': serial(global_rect(overlay)),
              'card_local': serial(overlay.card.geometry()), 'errors': [], 'scrolls': []}
    if not isinstance(candidate, QWidget) or not isValid(candidate):
        result['errors'].append('target_missing')
        result['passed'] = False
        return result
    target = candidate
    result['target_class'] = type(target).__name__
    result['target_name'] = target.objectName()
    origin = overlay.mapToGlobal(QPoint(0, 0))
    raw = global_rect(target)
    visible = QRect(raw)
    ancestor = target.parentWidget()
    while ancestor is not None:
        visible = visible.intersected(global_rect(ancestor))
        if isinstance(ancestor, QScrollArea):
            viewport = global_rect(ancestor.viewport())
            content = ancestor.widget()
            belongs = content is not None and (content is target or content.isAncestorOf(target))
            if belongs:
                fits = raw.width() <= viewport.width() and raw.height() <= viewport.height()
                result['scrolls'].append({'viewport_global': serial(viewport),
                    'fits_target': fits, 'fully_revealed': viewport.contains(raw),
                    'horizontal': ancestor.horizontalScrollBar().value(),
                    'vertical': ancestor.verticalScrollBar().value()})
                if fits and not viewport.contains(raw):
                    result['errors'].append('small_target_not_revealed')
        if ancestor is overlay.host:
            break
        ancestor = ancestor.parentWidget()
    visible = visible.intersected(global_rect(overlay))
    hole = overlay.spotlight_rect.translated(origin)
    result.update(target_global=serial(raw), visible_global=serial(visible),
                  spotlight_global=serial(hole),
                  target_local=serial(raw.translated(-origin)),
                  visible_local=serial(visible.translated(-origin)),
                  spotlight_local=serial(overlay.spotlight_rect))
    if not target.isVisibleTo(overlay.host):
        result['errors'].append('target_not_visible')
    if visible.isEmpty() or visible.width() < min(12, raw.width()) or visible.height() < min(12, raw.height()):
        result['errors'].append('target_clipped_away')
    # A small border tolerance permits the painted rounded outline while still
    # rejecting a hole positioned over an unrelated empty portion of a dialog.
    if hole.isEmpty() or not hole.adjusted(3, 3, -3, -3).intersects(visible):
        result['errors'].append('spotlight_misses_target')
    expected = visible.intersected(global_rect(overlay).adjusted(3, 3, -3, -3))
    if not hole.adjusted(-2, -2, 2, 2).contains(expected):
        result['errors'].append('spotlight_does_not_cover_visible_target')
    if not visible.isEmpty() and not visible.adjusted(-8, -8, 8, 8).contains(hole):
        result['errors'].append('spotlight_is_not_tied_to_target_bounds')
    if not overlay.rect().contains(overlay.card.geometry()):
        result['errors'].append('tutorial_card_outside_host')
    card_global = global_rect(overlay.card)
    if not visible.isEmpty() and card_global.intersects(visible):
        result['errors'].append('tutorial_card_covers_target')
    result['passed'] = not result['errors']
    return result


def capture_onboarding(window, report_path):
    """Exercise the actual frozen tour and persistence in the smoke data space."""
    app = QApplication.instance()
    app.setActiveWindow(window)
    manager = window.onboarding
    if not manager.show('dashboard'):
        raise RuntimeError('The packaged onboarding entry could not be opened')
    app.processEvents()
    overlay = manager.overlay
    count = len(overlay.steps)
    screenshot = Path(report_path).with_name(Path(report_path).stem + '-onboarding.png')
    if overlay.spotlight_rect.isEmpty() or not overlay.rect().contains(overlay.card.geometry()):
        raise RuntimeError('The packaged onboarding spotlight/card is not visible')
    if not window.grab().save(str(screenshot)):
        raise RuntimeError('The packaged onboarding screenshot could not be written')
    for _ in range(count):
        overlay.next_button.click()
        app.processEvents()
    progress = json.loads((window.data_dir / 'ui-onboarding.json').read_text('utf-8'))
    if manager.overlay is not None or progress.get('seen', {}).get('dashboard') != 'completed':
        raise RuntimeError('The packaged onboarding did not finish and save its progress')
    return {'passed': True, 'steps': count, 'progress_saved': True, 'screenshot': str(screenshot)}


def verify(data_dir, report_path):
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    from .branding import set_windows_identity, configure_application
    set_windows_identity()
    app = QApplication.instance() or QApplication([])
    configure_application(app)
    app.setStyle('Fusion')
    apply_appearance(app)
    window = MainWindow(data_dir)
    window.show()
    start = time.monotonic()
    state = {'loaded': False, 'platform': app.platformName(), 'screenshot': str(report_path.with_suffix('.png'))}
    def finish():
        if window.type_map and not window.bridge.callbacks:
            window.grab().save(str(report_path.with_suffix('.png')))
            state.update({'loaded': True, 'visible': window.isVisible(), 'epoch': window.bridge.epoch, 'revision': window.bridge.revision, 'elapsed_seconds': round(time.monotonic() - start, 3), 'appearance': current_appearance(), 'font_pixel_size': app.font().pixelSize(), 'has_application_icon': not app.windowIcon().isNull(), 'has_window_icon': not window.windowIcon().isNull()})
            try:
                state['onboarding'] = capture_onboarding(window, report_path)
            except Exception as error:
                state['onboarding'] = {'passed': False, 'error': str(error)}
        elif time.monotonic() - start < 20:
            QTimer.singleShot(250, finish)
            return
        report_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        QTimer.singleShot(1500, app.quit)
    QTimer.singleShot(1000, finish)
    app.exec()
    return 0 if state['loaded'] and state.get('onboarding', {}).get('passed') else 1
