"""Reproduce PNG/Windows ICO assets from the editable vector master."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtGui import QImage,QPainter
from PySide6.QtCore import Qt,QRectF
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'src'/'management'/'assets'
app=QApplication.instance() or QApplication([])
renderer=QSvgRenderer(str(ASSETS/'app-icon.svg'))
assert renderer.isValid()
canvas=QImage(2048,2048,QImage.Format.Format_ARGB32_Premultiplied);canvas.fill(Qt.GlobalColor.transparent)
painter=QPainter(canvas);renderer.render(painter,QRectF(0,0,2048,2048));painter.end()
output=ROOT/'.build'/'icon-master-render.png';canvas.save(str(output))
master=Image.open(output).convert('RGBA')
master.resize((1024,1024),Image.Resampling.LANCZOS).save(ASSETS/'app-icon.png')
sizes=[16,20,24,32,40,48,64,96,128,256]
master.resize((256,256),Image.Resampling.LANCZOS).save(ASSETS/'app-icon.ico',format='ICO',sizes=[(x,x) for x in sizes])
proof=Image.new('RGB',(1100,510),'#14161a');draw=ImageDraw.Draw(proof)
for x,bg in [(0,'#14161a'),(550,'#f1f3f6')]:
 draw.rectangle((x,0,x+549,509),fill=bg)
 big=master.resize((280,280),Image.Resampling.LANCZOS);proof.paste(big,(x+135,30),big)
 xpos=x+50
 for size in [16,24,32,48,64,96]:
  small=master.resize((size,size),Image.Resampling.LANCZOS);proof.paste(small,(xpos,378-size//2),small)
  draw.text((xpos,443),str(size),fill='#a4adba' if x==0 else '#455065')
  xpos+=size+20
proof.save(ROOT/'.build'/'app-icon-proof.png')
print('Vector master, 1024px PNG, and 10-size ICO generated.')
