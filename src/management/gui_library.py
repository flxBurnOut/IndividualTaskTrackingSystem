"""Open readable folders after bounded asynchronous materialization batches."""
from PySide6.QtCore import QObject,QTimer,QUrl
from PySide6.QtGui import QDesktopServices
from shiboken6 import isValid


class LibraryOpener(QObject):
    def __init__(self,bridge,parent,button,owner_id,on_error):
        super().__init__(parent)
        self.bridge,self.button,self.owner_id,self.on_error=bridge,button,owner_id,on_error
        self.original_text=button.text();self.processed=0;self.issues=0;self.done=False
        button.setEnabled(False);button.setText('准备文件夹…');self.next(0)

    def alive(self):return not self.done and isValid(self) and isValid(self.button)

    def next(self,offset):
        if not self.alive():return
        self.bridge.query('library_folder',self.loaded,self.failed,owner_id=self.owner_id,offset=offset,limit=20)

    def loaded(self,result):
        if not self.alive():return
        self.processed+=result['processed'];self.issues+=len(result.get('issues',[]))
        if result.get('next_offset') is not None:
            self.button.setText(f"准备文件夹 {self.processed}/{result['total']}…")
            QTimer.singleShot(0,lambda:self.next(result['next_offset']));return
        self.finish()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(result['path'])):
            self.on_error({'message':'系统暂时无法打开文件夹。目录：'+result['path']})
        elif self.issues:
            self.on_error({'message':f'{self.issues} 份文件需要核对，现有文件与数据库记录均已保留；可单独打开资料查看原因。'})

    def failed(self,error):
        if not self.alive():return
        self.finish();self.on_error(error)

    def finish(self):
        self.done=True
        if isValid(self.button):self.button.setEnabled(True);self.button.setText(self.original_text)
        self.deleteLater()


def open_library(bridge,parent,button,owner_id,on_error):
    operation=LibraryOpener(bridge,parent,button,owner_id,on_error)
    # Parent ownership keeps the operation alive until its asynchronous result.
    return operation
