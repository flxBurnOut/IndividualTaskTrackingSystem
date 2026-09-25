"""Resumable extraction batches in a disposable, resource-limited process."""
from __future__ import annotations
import codecs
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from .source_worker import decode, html_text, PlainHTML

TEXT_CHARS=2400
BATCH_CHARS=64000
BATCH_UNITS=40
BATCH_IMAGES=4
MAX_ZIP_BYTES=128*1024*1024


def _image(path, workspace, index, *, frame=0):
    from PIL import Image,ImageOps
    Image.MAX_IMAGE_PIXELS=24_000_000
    with Image.open(path) as picture:
        if picture.width*picture.height>24_000_000: raise ValueError('图片超过单批像素保护值')
        picture.seek(frame)
        result=ImageOps.exif_transpose(picture).convert('RGB');result.thumbnail((1600,1600))
        output=workspace/('visual-'+str(index)+'.png');result.save(output,'PNG')
    return output


def _paragraphs(stream):
    # iterparse releases elements as they finish; tables retain tabs and row
    # boundaries. No archive member is expanded into an unbounded XML tree.
    parts=[];length=0
    for _,node in ET.iterparse(stream,events=('end',)):
        tag=node.tag.rsplit('}',1)[-1]
        if tag in {'t','v'} and node.text: parts.append(node.text);length+=len(node.text)
        elif tag in {'tab','tc','c'}:parts.append('\t')
        elif tag in {'p','tr','row'}:parts.append('\n')
        if length>=TEXT_CHARS:
            yield ''.join(parts);parts=[];length=0
        node.clear()
    if parts:yield ''.join(parts)


def _pdf_image(source,number,workspace):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication,QColor,QImage,QPainter
    from PySide6.QtPdf import QPdfDocument
    app=QGuiApplication.instance() or QGuiApplication([])
    pdf=QPdfDocument();pdf.load(str(source));shape=pdf.pagePointSize(number)
    scale=min(2.0,1600/max(shape.width(),shape.height(),1))
    image=pdf.render(number,QSize(max(1,int(shape.width()*scale)),max(1,int(shape.height()*scale))))
    if image.isNull():raise ValueError('PDF 页面无法渲染')
    paper=QImage(image.size(),QImage.Format.Format_RGB32);paper.fill(QColor('white'))
    painter=QPainter(paper);painter.drawImage(0,0,image);painter.end()
    output=workspace/f'pdf-{number+1}.png';paper.save(str(output),'PNG');pdf.close()
    return output


def units(source,name,workspace,*,layout=None,start=0,depth=0):
    """Yield (stable natural-unit index, locator, kind, content), without limits
    on whole-document page/character totals. Bounds apply in parse_batch only."""
    suffix=Path(name).suffix.lower();sequence=0
    def text_units(text,label):
        for offset in range(0,len(text),TEXT_CHARS):
            yield label+f' / 字符 {offset+1}-{min(len(text),offset+TEXT_CHARS)}','text',text[offset:offset+TEXT_CHARS]
    if suffix in {'.txt','.md','.csv','.tsv','.log','.json','.html','.htm'}:
        with source.open('rb') as stream: initial=stream.read(8192)
        encoding='utf-16' if initial.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'
        try: initial.decode(encoding)
        except UnicodeDecodeError:
            try: codecs.getincrementaldecoder(encoding)().decode(initial,final=False)
            except UnicodeDecodeError: encoding='gb18030'
        if suffix in {'.html','.htm'}:
            parser=PlainHTML()
            with source.open('r',encoding=encoding,errors='strict') as stream:
                while raw:=stream.read(TEXT_CHARS):
                    parser.feed(raw);text=parser.text();parser.parts=[]
                    if sequence>=start: yield sequence,f'网页正文段 {sequence+1}','text',text
                    sequence+=1
            return
        with source.open('r',encoding=encoding,errors='strict',newline='') as stream:
            header=stream.readline() if suffix in {'.csv','.tsv'} else ''
            if header:
                if start==0:yield sequence,'表头（后续段沿用这些列）','text',header
                sequence+=1
            while text:=stream.read(TEXT_CHARS):
                if sequence>=start:yield sequence,f'正文段 {sequence+1}','text',text
                sequence+=1
        return
    if suffix=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(''): raise ValueError('PDF 需要密码，原件已保留')
        # A page has a stable sequence range. Resume jumps to the exact page.
        page_start=start//1_000_000
        for number in range(page_start,len(reader.pages)):
            page=reader.pages[number];text=page.extract_text() or ''
            blocks=list(text_units(text,f'PDF 第 {number+1} 页'))
            resources=page.get('/Resources');resources=resources.get_object() if resources else {}
            drawing=page.get_contents()
            vector=bool(drawing is not None and re.search(rb'(?:^|\s)(?:m|l|c|v|y|re|sh)\s',drawing.get_data()))
            visual=layout=='timetable' or len(text.strip())<20 or bool(resources.get('/XObject')) or vector
            for part,(label,kind,value) in enumerate(blocks):
                index=number*1_000_000+part
                if index>=start: yield index,label,kind,value
            if visual:
                index=number*1_000_000+999_998
                if index>=start: yield index,f'PDF 第 {number+1} 页图像','image',_pdf_image(source,number,workspace)
            if not blocks and not visual:
                index=number*1_000_000+999_999
                if index>=start: yield index,f'PDF 第 {number+1} 页（空白）','text',''
        return
    if suffix in {'.docx','.pptx','.xlsx'}:
        with zipfile.ZipFile(source) as archive:
            infos=archive.infolist()
            if sum(i.file_size for i in infos)>MAX_ZIP_BYTES or len(infos)>20000:
                raise ValueError('文档展开量超过读取器资源保护值；已保留原件与进度')
            if suffix=='.docx':
                names=['word/document.xml']+sorted(n for n in archive.namelist() if re.fullmatch(r'word/(header\d+|footer\d+|footnotes|endnotes|comments)\.xml',n))
            elif suffix=='.pptx':
                names=sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',n)),key=lambda n:int(re.search(r'slide(\d+)',n)[1]))
                names+=sorted(n for n in archive.namelist() if re.fullmatch(r'ppt/notesSlides/notesSlide\d+\.xml',n))
            else:
                # Shared string resolution and formulas are handled by openpyxl.
                from openpyxl import load_workbook
                original=source.open('rb')
                book=load_workbook(original,read_only=True,data_only=False)
                try:
                    for sheet in book:
                        for number,row in enumerate(sheet.iter_rows(values_only=True),1):
                            text='\t'.join('' if value is None else str(value) for value in row)
                            for label,kind,value in text_units(text,f'{sheet.title} / 第 {number} 行'):
                                if sequence>=start: yield sequence,label,kind,value
                                sequence+=1
                finally:book.close();original.close()
                names=[]
            for name in names:
                with archive.open(name) as stream:
                    for paragraph,text in enumerate(_paragraphs(stream),1):
                        for label,kind,value in text_units(text,f'{name} / 段 {paragraph}'):
                            if sequence>=start: yield sequence,label,kind,value
                            sequence+=1
            extras=sorted(n for n in archive.namelist() if any('/'+part+'/' in n for part in ('media','embeddings','charts')) and not n.endswith('/'))
            for number,member in enumerate(extras):
                chart='/charts/' in member and member.endswith('.xml')
                width=2 if chart else 1
                if sequence+width<=start: sequence+=width;continue
                temporary=workspace/f'embedded-{number}{Path(member).suffix}'
                with archive.open(member) as src,temporary.open('wb') as dst:
                    import shutil
                    shutil.copyfileobj(src,dst,1024*1024)
                if Path(member).suffix.lower() in {'.png','.jpg','.jpeg','.gif','.bmp','.tif','.tiff','.webp'}:
                    yield sequence,member,'image',_image(temporary,workspace,sequence)
                elif '/charts/' in member and member.endswith('.xml'):
                    with temporary.open('rb') as stream: text=''.join(_paragraphs(stream))
                    if sequence>=start:yield sequence,member+' / 图表数据','text',text
                    sequence+=1
                    if sequence>=start:yield sequence,member+' / 图表布局','gap','已提取图表数据，但此内嵌图表未渲染布局；原件已完整保留。'
                else:
                    yield sequence,member,'gap','原件内已保留该嵌入对象；当前解析器不支持 '+Path(member).suffix
                sequence+=1
        return
    if suffix=='.eml':
        from email.parser import BytesParser
        from email import policy
        with source.open('rb') as stream: message=BytesParser(policy=policy.default).parse(stream)
        headers='\n'.join(f'{key}: {message.get(key,"")}' for key in ('Subject','From','To','Date'))
        body=message.get_body(preferencelist=('plain','html'))
        text=body.get_content() if body else ''
        if body and body.get_content_type()=='text/html':text=html_text(text)
        for label,kind,value in text_units(headers+'\n\n'+text,'邮件正文'):
            if sequence>=start:yield sequence,label,kind,value
            sequence+=1
        for number,part in enumerate(message.iter_attachments()):
            attachment_name=str(part.get_filename() or 'attachment.bin');suffix=Path(attachment_name).suffix
            temporary=workspace/f'mail-{depth}-{number}{suffix}';temporary.write_bytes(part.get_payload(decode=True) or b'')
            if depth>=3:
                if sequence>=start:yield sequence,attachment_name,'gap','邮件嵌套超过三层；原始附件已保留'
                sequence+=1;continue
            for _,label,kind,value in units(temporary,attachment_name,workspace,layout=layout,depth=depth+1):
                if sequence>=start:yield sequence,f'邮件附件 {attachment_name} / {label}',kind,value
                sequence+=1
        return
    if suffix in {'.png','.jpg','.jpeg','.webp','.bmp','.gif','.tif','.tiff'}:
        from PIL import Image
        with Image.open(source) as image:frames=getattr(image,'n_frames',1)
        for frame in range(start,frames):
            yield frame,f'图像帧 {frame+1}','image',_image(source,workspace,frame,frame=frame)
        return
    if not start:yield 0,'原文件','gap','当前没有此格式的解析器：'+suffix


def parse_batch(source,name,workspace,*,cursor=None,layout=None,batch_units=BATCH_UNITS):
    source=Path(source);workspace=Path(workspace);cursor=cursor or {}
    start=int(cursor.get('unit',0));offset=int(cursor.get('offset',0))
    chunks=[];characters=0;images=0;done=0;complete=True;following=cursor;gaps=0
    iterator=units(source,name,workspace,layout=layout,start=start)
    for index,label,kind,value in iterator:
        if done>=max(1,min(BATCH_UNITS,int(batch_units))) or characters>=BATCH_CHARS or kind=='image' and images>=BATCH_IMAGES:
            following={'unit':index,'offset':0};complete=False;break
        if kind=='image':
            output=workspace/f'chunk-{len(chunks)}.png'
            if Path(value)!=output:
                import shutil
                shutil.copyfile(value,output)
            chunks.append({'kind':kind,'locator':label,'path':output.name});images+=1
        else:
            raw=str(value)
            position=offset if index==start else 0
            while position<len(raw) or not raw and position==0:
                if characters>=BATCH_CHARS or len(chunks)>=80:
                    following={'unit':index,'offset':position};complete=False;break
                text=raw[position:position+TEXT_CHARS]
                output=workspace/f'chunk-{len(chunks)}.txt';output.write_bytes(text.encode('utf-8'))
                chunks.append({'kind':kind,'locator':label+(f' / 续段 {position}' if position else ''),'path':output.name})
                characters+=len(text);position+=len(text)
                if not raw:break
            if not complete:break
            if kind=='gap':gaps+=1
        done+=1;following={'unit':index+1,'offset':0}
    iterator.close()
    return {'ok':True,'chunks':chunks,'cursor':following,'finished':complete,'units_done':done,'characters':characters,
            'coverage':{'text_extracted':True,'images_extracted':images,'unsupported_units':gaps,
                        'text_and_visuals_require_analysis':True}}
