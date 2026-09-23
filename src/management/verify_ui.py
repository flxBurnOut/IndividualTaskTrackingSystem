"""Release smoke harness for our own Qt window, without desktop capture."""
from pathlib import Path
import json
import time
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from .gui import MainWindow
from .gui_theme import apply_appearance, current_appearance


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
        elif time.monotonic() - start < 20:
            QTimer.singleShot(250, finish)
            return
        report_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        QTimer.singleShot(1500, app.quit)
    QTimer.singleShot(1000, finish)
    app.exec()
    return 0 if state['loaded'] else 1
