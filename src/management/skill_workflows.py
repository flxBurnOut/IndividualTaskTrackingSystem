"""Versioned built-in instruction packages, selected only from the user's message."""
from pathlib import Path
import hashlib
import re
from .schemas import BusinessError

CATALOG={'course-notes':{'title':'整理课件笔记','version':1,'path':'course-notes/SKILL.md'}}

def select_skill(payload):
    explicit=payload.get('skill_id')
    if explicit is not None and explicit not in CATALOG:
        raise BusinessError('unknown_skill','此笔记技能尚未安装，请选择软件提供的技能。')
    if explicit:return explicit
    text=payload.get('text','')
    if re.search(r'(整理|生成|制作|更新|修订).{0,24}笔记|笔记.{0,24}(整理|更新|修订)|(?:course|lecture|study) notes',text,re.I):
        return 'course-notes'
    return None

def skill_context(identifier):
    if identifier not in CATALOG:raise BusinessError('unknown_skill','未注册的笔记技能。')
    info=CATALOG[identifier];path=Path(__file__).with_name('builtin_skills')/info['path']
    text=path.read_text(encoding='utf-8')
    if len(text.encode('utf-8'))>16000:raise BusinessError('skill_limit','技能内容超过本轮指令预算。')
    return {'id':identifier,'title':info['title'],'version':info['version'],'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'instructions':text,'scope':'Only this explicitly selected or matching user request; not future unrelated requests.'}
