"""Shared visual identity for the native application and Windows shortcuts."""
from pathlib import Path
import ctypes
import os

APP_NAME = '个人事务管理'
APP_USER_MODEL_ID = 'PersonalManagement.Desktop'


def icon_path():
    return Path(__file__).resolve().parent / 'assets' / 'app-icon.ico'


def set_windows_identity():
    if os.name != 'nt':
        return False
    function = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_long
    return function(APP_USER_MODEL_ID) == 0


def configure_application(app):
    from PySide6.QtGui import QIcon
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName('PersonalManagement')
    app.setWindowIcon(QIcon(str(icon_path())))
