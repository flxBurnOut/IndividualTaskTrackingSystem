"""Shared unsaved-edit guard for update and tray exit requests."""
from PySide6.QtWidgets import QApplication


def shutdown_blocker(window, allowed_dialog=None):
    others = [widget for widget in QApplication.topLevelWidgets()
              if widget.isVisible() and widget not in (window, allowed_dialog)]
    if others or window.review_pending:
        return '请先保存并关闭其他编辑窗口，再退出软件与后台。尚未停止后台。'
    if window.bridge.callbacks or window.bridge.uncertain_writes:
        return '仍有读取、保存或待核对的保存回执，请等处理结束后重试。'
    return ''


def request_exit(window):
    if getattr(window, '_exit_pending', False):
        return
    problem = shutdown_blocker(window)
    if problem:
        window.show_error({'message': problem})
        return
    window._exit_pending = True
    window.poll.stop()
    window.codex_connection.timer.stop()
    window.notice.setText('正在检查后台任务并退出…')
    window.notice.show()
    def done(_):
        window.codex_connection.request_stop()
        window.close()
    def failed(error):
        window._exit_pending = False
        window.show_error(error)
        if error.get('code') not in {'update_draining', 'update_pending'}:
            window.poll.start()
            window.codex_connection.request_check()
    window.bridge.stop_service(done, failed)
