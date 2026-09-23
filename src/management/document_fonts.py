"""Find a locally installed, embeddable font; never ship proprietary system fonts."""
import os
from pathlib import Path
import sys

from .resources import ResourceError


def pdf_font(text):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont, TTFError
    candidates = []
    if os.environ.get('PERSONAL_MANAGEMENT_PDF_FONT'):
        candidates.append(Path(os.environ['PERSONAL_MANAGEMENT_PDF_FONT']).expanduser())
    if sys.platform == 'darwin':
        candidates += [Path('/System/Library/Fonts/Supplemental/Songti.ttc'),
                       Path('/System/Library/Fonts/STHeiti Light.ttc'),
                       Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')]
    elif os.name == 'nt':
        candidates.append(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'simsun.ttc')
    required = {ord(char) for char in text if not char.isspace()}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            font = TTFont('PMDocument', str(path), subfontIndex=0)
            if not required.issubset(font.face.charToGlyph):
                continue
            pdfmetrics.registerFont(font)
            return font.fontName
        except (OSError, TTFError):
            continue
    if any(code > 127 for code in required):
        raise ResourceError('FONT_REQUIRED', '没有覆盖正文字符的可嵌入字体。请通过 PERSONAL_MANAGEMENT_PDF_FONT 指定 TTF/TTC 字体。')
    return 'Helvetica'
