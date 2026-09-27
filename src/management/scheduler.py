"""Durable single-worker jobs and occurrence-deduplicated local schedules."""
from __future__ import annotations
import calendar
import datetime as dt
import json
import logging
import threading
import time
from zoneinfo import ZoneInfo
from .storage import encode, new_id, now
from . import conversations, conversation_progress


class Background:
    def __init__(self, core, stop):
        self.core, self.stop = core, stop
        self.threads = []

    def start(self):
        with self.core.store.lock, self.core.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            from .session_coordinator import recover_candidates
            recover_candidates(self.core,c)
            from .native_desktop import recover as recover_desktop
            recover_desktop(self.core,c)
            from .session_coordinator import recover_pending_native_candidates
            recover_pending_native_candidates(self.core,c)
            from .context_driver import recover as recover_context
            recover_context(self.core,c)
            from .context_driver import upgrade_queued
            upgrade_queued(self.core,c)
            c.execute("UPDATE io_operations SET status='needs_reconciliation' WHERE status='preparing'")
            c.execute("""UPDATE jobs SET status='failed',generation=generation+1,error=?,updated_at=? WHERE status='running'
                AND coalesce(json_extract(input,'$.desktop_transport'),'')!='native_ipc_v1'""", (encode({'code': 'interrupted', 'message': '服务在执行中断开。未自动重试，请核对已保存的产物后重新发起。'}), now()))
            conversations.reconcile(self.core, c)
            c.commit()
        for function in (self.worker, self.scheduler):
            thread = threading.Thread(target=function, daemon=True)
            self.threads.append(thread)
            thread.start()

    def worker(self):
        while not self.stop.is_set():
            job = None
            with self.core.store.lock, self.core.store.connect() as c:
                if self.stop.is_set():
                    return
                row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY updated_at,created_at LIMIT 1").fetchone()
                if row:
                    job = dict(row)
                    cancel = threading.Event()
                    self.core.cancel_events[job['id']] = cancel
                    c.execute('BEGIN IMMEDIATE')
                    c.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job['id']))
                    conversations.mark_running(c, job)
                    conversation_progress.start(c, job)
                    c.commit()
            if job is None:
                if time.monotonic()-getattr(self,'_native_checked',0)>20:
                    self._native_checked=time.monotonic()
                    from .session_coordinator import check_native_turns
                    check_native_turns(self.core)
                from .materials import idle_batch
                idle_batch(self.core)
                self.stop.wait(.3)
                continue
            native_execution = False
            try:
                if cancel.is_set() or self.stop.is_set():
                    continue
                value = json.loads(job['input'])
                if job['kind'] == 'ai':
                    from .ai import generate
                    settings = self.core.query('settings')['settings']
                    if isinstance(value.get('ai_config'),dict): settings['ai']=value['ai_config']
                    settings['_discussion_epoch'] = job['epoch']
                    settings['_codex_project_dir'] = str(self.core.root/'Codex事务助手')
                    settings['_codex_bridge_dir'] = str(self.core.root/'codex-desktop-bridge')
                    progress = conversation_progress.Publisher(self.core, job, cancel, self.stop)
                    if settings.get('ai', {}).get('execution_mode') == 'desktop_shared':
                        native_execution = True
                        from .native_desktop import generate as generate_desktop
                        result = generate_desktop(self.core, job, value, settings, cancel, self.stop, progress)
                    else:
                        result = generate(value, settings, cancel, progress)
                    if result.get('context_continuation'):
                        with self.core.store.lock,self.core.store.connect() as c:
                            c.execute('BEGIN IMMEDIATE')
                            from .context_driver import requeue
                            current=self.core._job(c,job['id'])
                            if current['status']=='running' and current['generation']==job['generation'] and not cancel.is_set():
                                requeue(self.core,c,current,provider=result.get('provider'))
                            c.commit()
                        continue
                    status = 'completed' if value.get('conversation_id') and not result.get('actions') else 'awaiting_review'
                else:
                    self.core.resources.create_workspace(job['id'])
                    if value['kind'] in {'docx', 'pdf', 'pdf_notebook'}:
                        from .document_worker import produce
                        frozen = produce(self.core.resources, job['id'], value, cancel)
                    else:
                        self.core.resources.generate_artifact(job['id'], value['relative_path'], value['kind'], value['content'])
                        if cancel.is_set():
                            continue
                        frozen = self.core.resources.freeze_artifact(job['id'], value['relative_path'], kind=value['kind'], source_versions=value.get('source_versions', []))
                    result, status = {'metadata': frozen}, 'completed'
                with self.core.store.lock, self.core.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    current = self.core._job(c, job['id'])
                    if current['generation'] != job['generation'] or current['status'] != 'running' or current['epoch'] != self.core.store.meta(c, 'epoch') or cancel.is_set() or self.stop.is_set():
                        c.rollback()
                        continue
                    if job['kind'] == 'artifact':
                        entity = self.core._create(c, {'type': 'artifact', 'title': value.get('title') or value['relative_path'], 'status': 'draft', 'data': {**result['metadata'], 'job_id': job['id'], 'original_name': value['relative_path'].split('/')[-1], 'generated_at': now(), 'adopted_at': None, 'externally_submitted': False}}, 'job:' + job['id'])
                        if value.get('owner_id'):
                            self.core._dispatch(c, 'link', {'source_id': value['owner_id'], 'target_id': entity['id'], 'kind': 'contributes'}, 'job:' + job['id'])
                        result['entity'] = entity
                        self.core.store.set_meta(c, 'revision', self.core.store.meta(c, 'revision') + 1)
                    if job['kind']=='ai' and not result.get('_candidate_validated'):
                        if value.get('context_operation_id'):
                            from .context_service import _operation,validate
                            result,proof=validate(self.core,c,_operation(self.core,c,value['context_operation_id']),result)
                            result.update(_candidate_validated=True,context_validation=proof)
                            c.execute("UPDATE context_operations SET phase='ready' WHERE id=?",(value['context_operation_id'],))
                        elif value.get('plan_requested'):
                            from .plan_assistance import normalize
                            result=normalize(self.core,c,current,result)
                    c.execute('UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?', (status, encode(result), now(), job['id']))
                    if job['kind'] == 'ai':
                        conversations.complete_job(self.core, c, job, result)
                    conversation_progress.finish(c, job['id'])
                    c.commit()
            except Exception as error:
                from .native_desktop import ServiceDetached
                if isinstance(error, ServiceDetached) or (native_execution and self.stop.is_set()):
                    # The desktop owns its turn. Retain the same running job for
                    # startup reconciliation; closing a service is not cancellation.
                    continue
                with self.core.store.lock, self.core.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    failure = {'code': getattr(error, 'code', 'executor_error'), 'message': getattr(error, 'message', str(error))[:1000]}
                    # Persist bounded protocol metadata, never raw stderr/prompts or credentials.
                    detail = getattr(error, 'details', {})
                    if isinstance(detail, dict):
                        safe = {k: detail[k] for k in ('stage', 'elapsed_seconds', 'timeout_seconds', 'request_method', 'last_event', 'protocol_code', 'reason')
                                if k in detail and isinstance(detail[k], (str, int, float)) and len(str(detail[k])) <= 100}
                        if safe: failure['details'] = safe
                    from .session_coordinator import get_candidate,complete_candidate,settle
                    candidate=get_candidate(c,job['id'])
                    current=self.core._job(c,job['id'])
                    if candidate and current['status']=='running' and current['generation']==job['generation'] and current['epoch']==self.core.store.meta(c,'epoch') and not cancel.is_set():
                        if json.loads(current['input']).get('desktop_transport')=='native_ipc_v1':
                            # A saved candidate is not proof that the desktop's
                            # actual turn ended. Hand only this failed worker's
                            # wait to the read-only observer; never queue a send.
                            c.execute('UPDATE conversation_operations SET pending_terminal=1 WHERE job_id=?', (job['id'],))
                            c.execute('UPDATE jobs SET error=?,updated_at=? WHERE id=?', (encode(failure),now(),job['id']))
                        else:
                            complete_candidate(self.core,c,current,candidate)
                        c.commit()
                        continue
                    if not cancel.is_set() and current['status']=='running' and current['generation']==job['generation']:
                        from .context_driver import requeue
                        if requeue(self.core,c,current,error=failure):
                            c.commit()
                            self.stop.wait(min(30,5))
                            continue
                    if failure['code']=='AI_REQUEST_REJECTED':
                        settle(c,job['id'],'rejected',failure)
                    changed = c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status='running' AND generation=? AND epoch=?",
                        (encode(failure), now(), job['id'], job['generation'], self.core.store.meta(c, 'epoch'))).rowcount
                    if changed:
                        conversations.update_job(self.core, c, job, 'failed', failure)
                        conversation_progress.finish(c, job['id'])
                    c.commit()
            finally:
                self.core.cancel_events.pop(job['id'], None)

    def scheduler(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger('management').exception('Schedule tick failed')
            if time.monotonic()-getattr(self,'_context_pruned',0)>3600:
                from .context_driver import prune_caches
                prune_caches(self.core);self._context_pruned=time.monotonic()
            self.stop.wait(15)

    def tick(self, instant=None):
        instant = instant or dt.datetime.now(dt.timezone.utc)
        from .recurring import tick as recurring_tick
        try:
            recurring_result = recurring_tick(self.core, instant, after_id=getattr(self, '_recurring_cursor', None))
            self._recurring_cursor = recurring_result['next_after_id']
        except Exception:
            logging.getLogger('management').exception('Recurring preparation tick failed; continuing review schedules')
        with self.core.store.lock, self.core.store.connect() as c:
            settings = self.core.store.meta(c, 'settings')
            schedules = [self.core.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='schedule' AND archived=0 AND status NOT IN ('cancelled','draft')")]
            for schedule in schedules:
                d = schedule['data']
                if not d.get('enabled'):
                    continue
                local = instant.astimezone(ZoneInfo(d.get('timezone') or settings['timezone']))
                day = local.date().isoformat()
                if local.strftime('%H:%M') < d['time'] or d['frequency'] == 'weekly' and local.weekday() != d['weekday']:
                    continue
                occurrence = day + 'T' + d['time'] + '[' + str(local.tzinfo) + ']'
                if c.execute('SELECT 1 FROM schedule_runs WHERE schedule_id=? AND occurrence=?', (schedule['id'], occurrence)).fetchone():
                    continue
                rid = 'schedule:' + schedule['id'] + ':' + occurrence
                try:
                    c.execute('BEGIN IMMEDIATE')
                    created = None
                    if d['workflow'] == 'checkin':
                        from .reviews import query_daily
                        daily=query_daily(self.core,c,{'date':day})
                        if daily['needs_codex']:
                            text='今天没有明确的每日计划，请到 Codex 按实际情况复盘。'
                        elif daily['summary']['total'] == 0:
                            text='今天的计划没有待确认事项。'
                        else:
                            text=('到每日复盘时间了，请按当天计划和固定安排逐项记录；课程出勤与补课分别确认。' if daily.get('has_fixed_schedule') else '到每日复盘时间了，按今日计划逐项选择完成或未完成后确认。')
                    elif d['workflow'] == 'weekly_review':
                        start=(local.date()-dt.timedelta(days=6)).isoformat()
                        text='到每周回顾时间了。请在复盘中的每周回顾查看计划与反馈汇总，缺少计划的日期可交给 Codex 梳理。'
                    else:
                        risks = warning_scan(self.core, c, day)
                        text = '\n'.join(r['title'] + '：' + r['reason'] for r in risks[:50]) if risks else '已检查，当前没有符合已配置规则的新风险。'
                        if len(risks) > 50:
                            text += '\n另有 %d 项，请打开完整风险列表。' % (len(risks) - 50)
                    if d['workflow'] != 'warnings' or risks or d.get('notify_unchanged'):
                        self.core._create(c, {'type': 'notification', 'title': schedule['title'], 'data': {'content': text, 'business_date': day, 'seen': False, 'schedule_id': schedule['id'], 'occurrence': occurrence, 'target_id': created['id'] if created else None, 'review_mode': 'weekly' if d['workflow']=='weekly_review' else 'daily' if d['workflow']=='checkin' else None}}, rid)
                    c.execute('INSERT INTO schedule_runs VALUES (?,?,?,?)', (schedule['id'], occurrence, None, now()))
                    self.core.store.set_meta(c, 'revision', self.core.store.meta(c, 'revision') + 1)
                    c.commit()
                except Exception as error:
                    c.rollback()
                    # Make one durable failure visible instead of silently retrying
                    # every 15 seconds or inventing successful check-in delivery.
                    c.execute('BEGIN IMMEDIATE')
                    stamp, failed_job = now(), new_id()
                    message = getattr(error, 'message', str(error))[:1000]
                    c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (failed_job, 'schedule', 'failed', encode({'schedule_id': schedule['id'], 'occurrence': occurrence, 'title': schedule['title']}), None, encode({'code': getattr(error, 'code', 'schedule_error'), 'message': message}), self.core.store.meta(c, 'epoch'), self.core.store.meta(c, 'revision'), 1, stamp, stamp))
                    self.core._create(c, {'type': 'notification', 'title': schedule['title'] + '未完成', 'data': {'content': message + '。本次没有记录为执行成功，请核对配置后手动处理本次事项。', 'business_date': day, 'seen': False, 'schedule_id': schedule['id'], 'occurrence': occurrence, 'job_id': failed_job}}, rid)
                    c.execute('INSERT INTO schedule_runs VALUES (?,?,?,?)', (schedule['id'], occurrence, failed_job, stamp))
                    self.core.store.set_meta(c, 'revision', self.core.store.meta(c, 'revision') + 1)
                    c.commit()


# Both GUI queries and scheduled scans share the same scoped implementation.
from .planning import warning_scan
