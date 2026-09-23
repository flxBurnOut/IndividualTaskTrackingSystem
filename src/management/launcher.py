"""Choose a portable data space without copying or migrating business content."""
from pathlib import Path
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox, QLineEdit, QDialogButtonBox
from .runtime import default_data_dir


def choose_data_dir():
    from .branding import set_windows_identity, configure_application
    set_windows_identity()
    app = QApplication.instance() or QApplication([])
    configure_application(app)
    dialog = QDialog()
    dialog.setWindowTitle('选择数据空间')
    dialog.resize(570, 260)
    layout = QVBoxLayout(dialog)
    intro = QLabel('软件和业务数据分开保存。选择已有空间会直接打开；选择新目录会创建空空间。')
    intro.setWordWrap(True)
    layout.addWidget(intro)
    path = QLineEdit(str(default_data_dir()))
    layout.addWidget(path)
    browse = QPushButton('选择目录…')
    def select():
        value = QFileDialog.getExistingDirectory(dialog, '选择数据目录')
        if value:
            path.setText(value)
    browse.clicked.connect(select)
    layout.addWidget(browse)
    status = QLabel('')
    status.setWordWrap(True)
    layout.addWidget(status)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Open).setText('打开数据空间')
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
    def accept():
        selected = Path(path.text()).expanduser().absolute()
        if (selected / 'restore_pending.json').exists():
            status.setText('此目录的恢复尚未完成，请从原空间重新恢复到新的目录。')
            return
        if selected.exists() and not (selected / 'database.sqlite3').exists() and any(selected.iterdir()):
            status.setText('此目录已有其他文件。请选择已有软件数据空间或新建空目录，避免混入个人资料。')
            return
        dialog.selected = selected
        dialog.accept()
    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog.selected if dialog.exec() == QDialog.DialogCode.Accepted else None
