"""Capture owned source copies and inspect their explicitly bounded extraction."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QApplication,QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QTextEdit,QTextBrowser,QPushButton,QTabWidget,QWidget,QComboBox,QFileDialog,QListWidget,QListWidgetItem,QDialogButtonBox,QFrame)

SOURCE_KINDS={'file':'文件','notice':'通知','email':'邮件','image':'截图 / 图片','web':'网页'}
EXTRACTION_LABELS={'readable':'已提取文字，内容待核对','vision_ready':'图片已保存，需视觉核对','partial':'部分内容已提取','unsupported':'已保存，暂不支持文字提取','failed':'已保存，文字提取未成功'}

def extraction_label(entity):
    data=entity.get('data',entity)
    extraction=data.get('extraction') or entity.get('extraction') or {}
    return EXTRACTION_LABELS.get(extraction.get('status'),'已保存，尚无提取结果')

def source_origin(entity):
    data=entity.get('data',{})
    kind=SOURCE_KINDS.get(data.get('source_kind'),'资料')
    original=data.get('source_url') or data.get('original_name') or data.get('source_name') or ''
    if data.get('source_kind')=='web' and original:
        original=urlparse(original).netloc or original
    return kind+(' · '+str(original) if original else '')

def label(text,style=''):
    widget=QLabel(str(text));widget.setWordWrap(True);widget.setTextFormat(Qt.TextFormat.PlainText)
    if style: widget.setObjectName(style)
    return widget

def button(text,callback):
    widget=QPushButton(text);widget.clicked.connect(callback);return widget

def file_kind(path):
    suffix=Path(path).suffix.lower()
    return 'email' if suffix in {'.eml','.msg'} else 'image' if suffix in {'.png','.jpg','.jpeg','.webp','.bmp'} else 'file'

class AddSourceDialog(QDialog):
    """One capture surface. Clipboard is read only on the user's explicit action."""
    def __init__(self,bridge,owner_id=None,parent=None,on_saved=None,paths=None):
        super().__init__(parent)
        self.bridge,self.owner_id,self.on_saved=bridge,owner_id,on_saved
        self.epoch=bridge.epoch;self.pending=False;self.uncertain=False;self.temp_path=None;self.payloads=[];self.saved_sources=[]
        self.setWindowTitle('添加资料或通知');self.resize(650,565);self.setAcceptDrops(True)
        layout=QVBoxLayout(self);layout.addWidget(label('添加资料或通知','DialogHeading'))
        layout.addWidget(label('保存一份到软件中，之后原文件移动也能继续查看。提取结果会单独标明。','Quiet'))
        self.title=QLineEdit();self.title.setPlaceholderText('标题（可选，留空使用文件名或内容摘要）');layout.addWidget(self.title)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        files=QWidget();fl=QVBoxLayout(files);fl.addWidget(button('选择文件…',self.choose_files));self.files=QListWidget();fl.addWidget(self.files,1);fl.addWidget(label('也可把课件、文档或邮件文件拖到这里，软件会依次保存并分批读取。','Quiet'));self.tabs.addTab(files,'文件')
        words=QWidget();wl=QVBoxLayout(words);self.text_kind=QComboBox();self.text_kind.addItem('通知文字','notice');self.text_kind.addItem('邮件正文','email');wl.addWidget(self.text_kind);self.text=QTextEdit();self.text.setPlaceholderText('粘贴通知或邮件原文，保留日期、发送者和相关说明。');wl.addWidget(self.text);self.tabs.addTab(words,'文字')
        capture=QWidget();cl=QVBoxLayout(capture);cl.addWidget(button('粘贴剪贴板截图',self.paste_image));self.preview=label('先截图，然后点击上方按钮。','Quiet');self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter);self.preview.setMinimumHeight(170);cl.addWidget(self.preview,1);self.tabs.addTab(capture,'截图')
        web=QWidget();web_layout=QVBoxLayout(web);web_layout.addWidget(label('网页地址'));self.url=QLineEdit();self.url.setPlaceholderText('https://…');web_layout.addWidget(self.url);web_layout.addWidget(label('将尝试保存网页内容。需要登录或无法读取的页面会明确提示，可改为粘贴文字或截图。','Quiet'));web_layout.addStretch();self.tabs.addTab(web,'网页')
        self.status=label('','Error');self.status.hide();layout.addWidget(self.status)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel);self.save_button=self.buttons.button(QDialogButtonBox.StandardButton.Save);self.save_button.setText('保存资料');self.save_button.setObjectName('Primary');self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消');self.buttons.accepted.connect(self.save);self.buttons.rejected.connect(self.reject);layout.addWidget(self.buttons)
        if paths:self.set_files(paths)

    def set_files(self,paths):
        current=[self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]
        all_paths=list(dict.fromkeys(current+[str(Path(p).resolve()) for p in paths]))
        self.files.clear()
        for path in all_paths:
            item=QListWidgetItem(Path(path).name);item.setToolTip(path);item.setData(Qt.ItemDataRole.UserRole,path);self.files.addItem(item)
        self.tabs.setCurrentIndex(0)

    def choose_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择资料或邮件文件');self.set_files(paths)

    def dragEnterEvent(self,event):
        if not self.pending and event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):event.acceptProposedAction()
        else:event.ignore()

    def dropEvent(self,event):
        if not self.pending:self.set_files([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]);event.acceptProposedAction()

    def paste_image(self):
        image=QApplication.clipboard().image()
        if image.isNull():self.error({'message':'剪贴板中没有图片。请先截图或复制一张图片。'});return
        if image.width()*image.height()>24_000_000:self.error({'message':'图片过大，请裁剪后再粘贴。'});return
        self.cleanup_temp()
        fd,self.temp_path=tempfile.mkstemp(prefix='personal-management-capture-',suffix='.png');os.close(fd)
        if not image.save(self.temp_path,'PNG'):self.cleanup_temp();self.error({'message':'截图暂时无法保存。'});return
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(540,280,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation));self.status.hide()

    def build_payloads(self):
        base={'owner_id':self.owner_id}
        title=self.title.text().strip()
        if title:base['title']=title
        index=self.tabs.currentIndex()
        if index==0:
            paths=[self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]
            if not paths:raise ValueError('先选择要保存的文件。')
            return [dict(base,kind=file_kind(p),path=p) for p in paths]
        if index==1:
            text=self.text.toPlainText().strip()
            if not text:raise ValueError('先粘贴通知或邮件内容。')
            return [dict(base,kind=self.text_kind.currentData(),text=text)]
        if index==2:
            if not self.temp_path:raise ValueError('先粘贴一张截图。')
            return [dict(base,kind='image',path=self.temp_path)]
        url=self.url.text().strip()
        if urlparse(url).scheme not in {'http','https'} or not urlparse(url).netloc:raise ValueError('请输入完整的 http 或 https 网页地址。')
        return [dict(base,kind='web',url=url)]

    def save(self):
        if self.pending:return
        if not self.uncertain:
            try:self.payloads=self.build_payloads()
            except ValueError as exc:self.error({'message':str(exc)});return
        self.pending=True;self.tabs.setEnabled(False);self.title.setEnabled(False);self.save_button.setEnabled(False);self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
        self.status.setText('正在保存并检查可读取内容…');self.status.show();self.next_source()

    def next_source(self):
        if not self.payloads:
            self.pending=False;self.uncertain=False;self.cleanup_temp();self.accept();return
        payload=self.payloads[0]
        self.status.setText(f'已保存 {len(self.saved_sources)} 份，正在依次处理剩余 {len(self.payloads)} 份…')
        def saved(receipt):
            result=receipt.get('result',receipt);entity=result.get('entity',{})
            if entity:self.saved_sources.append(entity)
            self.payloads.pop(0);self.uncertain=False
            if self.on_saved:self.on_saved(receipt)
            self.next_source()
        self.bridge.command('add_source',payload,saved,self.error,epoch=self.epoch)

    def error(self,error):
        self.pending=False;self.uncertain=error.get('code')=='connection_lost'
        self.status.setText(error.get('message',str(error)));self.status.show();self.save_button.setEnabled(True)
        self.save_button.setText('核对并重试' if self.uncertain else '保存资料')
        self.tabs.setEnabled(not self.uncertain);self.title.setEnabled(not self.uncertain);self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)
        # Successful earlier files stay in the receipt list; retries use only remaining payloads.
        if self.payloads and not self.uncertain:
            remaining={p.get('path') for p in self.payloads}
            for i in reversed(range(self.files.count())):
                if self.files.item(i).data(Qt.ItemDataRole.UserRole) not in remaining:self.files.takeItem(i)

    def cleanup_temp(self):
        if self.temp_path and not self.pending and not self.uncertain:
            Path(self.temp_path).unlink(missing_ok=True);self.temp_path=None

    def reject(self):
        if self.pending:return
        self.cleanup_temp();super().reject()

    def closeEvent(self,event):
        if self.pending:event.ignore();return
        self.cleanup_temp();super().closeEvent(event)

class SourceContentDialog(QDialog):
    def __init__(self,bridge,entity,parent=None):
        super().__init__(parent);self.bridge,self.entity=bridge,entity;self.offset=0;self.pending=False
        self.setWindowTitle(entity.get('title','资料内容'));self.resize(730,620)
        layout=QVBoxLayout(self);layout.addWidget(label(entity.get('title','资料内容'),'DialogHeading'));self.status=label(extraction_label(entity),'Quiet');layout.addWidget(self.status);self.coverage=label('','Quiet');layout.addWidget(self.coverage);self.output=QTextBrowser();self.output.setOpenExternalLinks(False);layout.addWidget(self.output,1);self.more=button('继续读取',self.load);layout.addWidget(self.more);layout.addWidget(button('关闭',self.accept));self.load()
    def load(self):
        if self.pending:return
        self.pending=True;self.more.setEnabled(False)
        def loaded(result):
            self.pending=False;extraction=result.get('extraction') or {};coverage=result.get('coverage') or {};self.status.setText(EXTRACTION_LABELS.get(extraction.get('status'),'提取内容')+' · 以下内容仍需核对')
            details=[]
            if coverage.get('units_total') is not None:details.append('文字提取范围：'+str(coverage.get('units_read',0))+' / '+str(coverage['units_total'])+' 页 / 单元')
            if coverage.get('complete') is False:details.append('当前预览没有覆盖全部内容。')
            details.extend(str(x) for x in extraction.get('warnings',[]));self.coverage.setText('\n'.join(details));self.coverage.setVisible(bool(details))
            cursor=self.output.textCursor();cursor.movePosition(cursor.MoveOperation.End);cursor.insertText(result.get('text','') or ('没有可显示的文字。可打开保存的原资料。' if self.offset==0 else ''));self.offset=result.get('next_offset');self.more.setVisible(self.offset is not None);self.more.setEnabled(True)
        def failed(error):self.pending=False;self.status.setText(error.get('message',str(error)));self.more.setEnabled(True)
        self.bridge.query('source_content',loaded,failed,id=self.entity['id'],offset=self.offset or 0,limit=12000)

class SourcePickerDialog(QDialog):
    def __init__(self,bridge,owner_id=None,parent=None,selected=None):
        super().__init__(parent);self.bridge,self.owner_id=bridge,owner_id;self.selected={x['id']:x for x in selected or []};self.offset=0;self.loading=False;self.generation=0;self.dialogs=[]
        self.setWindowTitle('附带资料');self.resize(600,520);layout=QVBoxLayout(self);layout.addWidget(label('这次讨论参考哪些资料','DialogHeading'));layout.addWidget(label('资料将自动分批处理。取消勾选可从后续讨论中移除，不会删除原件。','Quiet'));self.items=QListWidget();self.items.itemChanged.connect(self.changed);layout.addWidget(self.items,1);self.status=label('','Quiet');layout.addWidget(self.status)
        row=QHBoxLayout();row.addWidget(button('添加资料或通知',self.add));self.more=button('加载更多',self.load);row.addWidget(self.more);row.addStretch();row.addWidget(button('使用这些资料',self.accept));layout.addLayout(row);self.load()
    def load(self):
        if self.loading:return
        self.loading=True;generation=self.generation;self.more.setEnabled(False)
        def loaded(result):
            if generation!=self.generation:return
            self.loading=False;self.items.blockSignals(True)
            ids={self.items.item(i).data(Qt.ItemDataRole.UserRole)['id'] for i in range(self.items.count())}
            for entity in result.get('items',[]):
                if entity['id'] in ids:continue
                item=QListWidgetItem(entity.get('title','资料')+'\n'+extraction_label(entity));item.setData(Qt.ItemDataRole.UserRole,entity);item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if entity['id'] in self.selected else Qt.CheckState.Unchecked);self.items.addItem(item)
            self.items.blockSignals(False);self.offset=result.get('next_offset');self.more.setVisible(self.offset is not None);self.more.setEnabled(True)
        self.bridge.query('sources',loaded,lambda e:(setattr(self,'loading',False),self.status.setText(e.get('message',str(e))),self.more.setEnabled(True)),owner_id=self.owner_id,offset=self.offset or 0,limit=30)
    def changed(self,item):
        entity=item.data(Qt.ItemDataRole.UserRole)
        if item.checkState()==Qt.CheckState.Checked:
            self.selected[entity['id']]=entity
        else:self.selected.pop(entity['id'],None)
        self.status.setText(f'已选 {len(self.selected)} 份 · 自动分批处理')
    def add(self):
        def saved(receipt):
            entity=receipt.get('result',receipt).get('entity',{})
            if entity:self.selected[entity['id']]=entity
            self.generation+=1;self.loading=False;self.offset=0;self.items.clear();self.load()
        dialog=AddSourceDialog(self.bridge,self.owner_id,self,saved);self.dialogs.append(dialog);dialog.open()
