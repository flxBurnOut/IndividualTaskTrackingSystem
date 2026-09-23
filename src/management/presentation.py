"""Human display context derived from facts, without rewriting source titles."""
from __future__ import annotations
import re

UNIT = re.compile(r'(?<![A-Za-z])(?P<kind>Tutorial|TUT|Lecture|Quiz|Practice\s+Questions?|Online\s+Questions?|PQ|OQ|Lab|Tutorials|Lectures)\s*(?:No\.?\s*|#)?(?P<number>\d{1,3}(?:\s*(?:[-–]|to)\s*\d{1,3})?[A-Za-z]?)(?![\d-])', re.I)
NAMES = {'tutorial':'Tutorial','tut':'Tutorial','tutorials':'Tutorial','lecture':'Lecture','lectures':'Lecture','quiz':'Quiz','practice question':'Practice Question','practice questions':'Practice Question','online question':'Online Question','online questions':'Online Question','pq':'PQ','oq':'OQ','lab':'Lab'}

class Presenter:
    def __init__(self, core, c):
        self.core, self.c, self.cache = core, c, {}

    def get(self, identifier):
        if not identifier:return None
        if identifier not in self.cache:
            row=self.c.execute('SELECT * FROM entities WHERE id=?',(identifier,)).fetchone()
            self.cache[identifier]=self.core.store.entity(row) if row else None
        return self.cache[identifier]

    def owner(self, entity):
        candidate=entity;fallback=None;seen=set()
        while candidate and candidate['id'] not in seen and len(seen)<64:
            seen.add(candidate['id'])
            if candidate['type']=='course':return candidate
            if fallback is None and candidate['type'] in {'project','activity','domain','goal'}:fallback=candidate
            next_id=candidate['data'].get('owner_id') if candidate['type']=='event' else None
            candidate=self.get(next_id or candidate.get('parent_id'))
        return fallback

    def fields(self, entity):
        owner=self.owner(entity)
        title=entity.get('title','')
        code=(owner['data'].get('code') or '') if owner else ''
        if owner and owner['type']=='course' and not code:
            match=re.search(r'\b[A-Z]{2,6}\d{4}\b',owner['title'],re.I)
            code=match.group().upper() if match else ''
        label=(code or owner['title']) if owner else '未归属'
        units=[]
        for source in (entity.get('data',{}).get('catchup_lesson_key') or '',title):
            for match in UNIT.finditer(source):
                name=NAMES[re.sub(r'\s+',' ',match['kind'].lower())]
                number=re.sub(r'\s+',' ',match['number'])
                number=re.sub(r'(?<!\d)0+(?=\d)','',number)
                value=name+' '+number
                if value not in units:units.append(value)
        unit_label='、'.join(units)
        display=(unit_label+' · '+title if unit_label and not UNIT.search(title) else title)
        return {'owner_id':owner['id'] if owner else None,'owner_type':owner['type'] if owner else None,
                'owner_title':owner['title'] if owner else '', 'owner_code':code,'owner_label':label,
                'display_title':display,'learning_unit_label':unit_label,
                'learning_unit_missing':bool(entity.get('data',{}).get('catchup_enabled') and not unit_label)}

    def entity(self, entity):
        return {**entity,**self.fields(entity)}

# Correlated ancestry lookup is used only for compact SQL sorting metadata.
# Whole entity bodies are loaded only for the requested page.
def owner_sort_sql(entity_alias='e'):
    return """coalesce((WITH RECURSIVE ancestry(id,parent_id,type,title,data,depth) AS (
      SELECT id,parent_id,type,title,data,0 FROM entities WHERE id="""+entity_alias+""".parent_id
      UNION ALL SELECT p.id,p.parent_id,p.type,p.title,p.data,a.depth+1 FROM entities p JOIN ancestry a ON p.id=a.parent_id WHERE a.depth<63)
      SELECT coalesce(nullif(json_extract(data,'$.code'),''),title)||char(0)||id FROM ancestry WHERE type IN ('course','project','activity','domain','goal')
      ORDER BY CASE type WHEN 'course' THEN 0 ELSE 1 END,depth LIMIT 1),'未归属')"""
