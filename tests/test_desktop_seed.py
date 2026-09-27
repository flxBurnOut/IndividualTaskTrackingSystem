"""Isolated subprocess fixtures: no Codex, model, real home or user thread."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from management import desktop_seed as seed


THREAD = 'fixture-thread-1'
FIXTURE = r'''
import json, os, pathlib, sys, time
mode, root = sys.argv[1], pathlib.Path(sys.argv[2])
path = root / 'rollout.jsonl'
read_requested = False
(root / 'startup.json').write_text(json.dumps({'argv':sys.argv, 'pid':os.getpid(),
    'injected_cli':os.environ.get('CODEX_CLI_PATH'), 'bridge':os.environ.get('PM_CODEX_BRIDGE_DIR'),
    'home':os.environ.get('CODEX_HOME')}))
def answer(rid, result):
    print(json.dumps({'id':rid, 'result':result}), flush=True)
def save():
    ident = 'other-thread' if mode == 'bad_header' else 'fixture-thread-1'
    path.write_text(json.dumps({'type':'session_meta','payload':{'id':ident}})+'\n')
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    with (root / 'requests.jsonl').open('a') as log:
        log.write(json.dumps(request)+'\n')
    if 'id' not in request:
        if mode == 'stop_reading' and method == 'initialized': time.sleep(60)
        continue
    rid = request['id']
    if method == 'initialize':
        answer(rid, {'userAgent':'isolated-fixture/1'})
    elif method == 'thread/start':
        if mode == 'start_timeout':
            time.sleep(60)
        elif mode == 'rejected':
            print(json.dumps({'id':rid,'error':{'code':-32602,'message':'private backend detail'}}),flush=True)
        elif mode == 'invalid_ack':
            answer(rid, {'thread':{'id':'../invalid'}})
        else:
            answer(rid, {'thread':{'id':'fixture-thread-1','path':str(path),'turns':[]},'model':'fixture-model'})
    elif method == 'thread/name/set':
        if mode == 'name_failure':
            print(json.dumps({'id':rid,'error':{'code':-1,'message':'private name detail'}}),flush=True)
        else: answer(rid,{})
    elif method == 'thread/read':
        read_requested = True
        if mode == 'read_failure':
            print(json.dumps({'id':rid,'error':{'code':-1,'message':'private read detail'}}),flush=True)
            continue
        if mode not in ('missing','eof_flush','bad_path'):
            save()
        answer(rid, {'thread':{'id':'other-thread' if mode=='wrong_id' else 'fixture-thread-1',
            'path':'relative.jsonl' if mode=='bad_path' else str(path),
            'turns':[{'id':'unexpected-turn'}] if mode=='not_empty' else []}})
    elif method == 'config/read':
        if mode == 'oversized':
            print('x'*10000,flush=True)
        elif mode == 'flood':
            for n in range(500): print(json.dumps({'method':'fixture/event','params':{'n':n}}),flush=True)
        elif mode == 'invalid_json':
            print('invalid JSON',flush=True)
        answer(rid,{'config':{'sandbox_mode':'read-only'},'layers':[]})
    elif method == 'thread/turns/list':
        answer(rid,{'data':[],'nextCursor':None})
    else: answer(rid,{})
(root / 'eof-seen').write_text('yes')
if mode == 'eof_flush' and read_requested:
    time.sleep(.1)
    save()
if mode == 'hang_eof': time.sleep(60)
'''


@pytest.fixture
def runtime(tmp_path):
    script = tmp_path / 'engine.py'
    script.write_text(FIXTURE, encoding='utf8')
    def options(mode='valid', **overrides):
        workspace = tmp_path / mode
        workspace.mkdir(exist_ok=True)
        environment = {**os.environ, 'CODEX_HOME': str(workspace / 'isolated-home'),
                       'CODEX_CLI_PATH': 'must-not-leak', 'PM_CODEX_BRIDGE_DIR': 'must-not-leak'}
        return workspace, {'command': [sys.executable, '-u', str(script), mode, str(workspace)],
            'environment': environment, 'timeout': 3, 'close_timeout': 1, **overrides}
    return options


def requests(workspace):
    return [json.loads(line) for line in (workspace / 'requests.jsonl').read_text().splitlines()]


def test_create_ack_is_saved_before_naming_reading_and_graceful_handoff(runtime):
    workspace, options = runtime()
    observed = []
    def save(ident):
        observed.append(ident)
        methods = [item['method'] for item in requests(workspace)]
        assert methods == ['initialize', 'initialized', 'thread/start']
        assert not (workspace / 'rollout.jsonl').exists()
    result = seed.create_empty_thread(workspace, 'project-1', 'Synthetic instruction',
        'fixture-model', save, title='Synthetic connection fixture', **options)
    assert observed == [THREAD]
    assert result['thread']['id'] == THREAD and result['handoff_ready'] is True
    assert Path(result['rollout_path']).is_file()
    assert (workspace / 'eof-seen').is_file()
    recorded = requests(workspace)
    assert [r['method'] for r in recorded] == [
        'initialize', 'initialized', 'thread/start', 'thread/name/set', 'thread/read']
    start = recorded[2]['params']
    assert start['projectId'] == 'project-1' and start['baseInstructions'] == 'Synthetic instruction'
    assert 'config' not in start and 'dynamicTools' not in start
    assert start['historyMode'] == 'legacy'
    assert recorded[-1]['params'] == {'threadId': THREAD, 'includeTurns': True}
    startup = json.loads((workspace / 'startup.json').read_text())
    assert startup['injected_cli'] is None and startup['bridge'] is None
    assert startup['home'] == str(workspace / 'isolated-home')


def test_rollout_flushed_only_after_eof_is_waited_for_before_handoff(runtime):
    workspace, options = runtime('eof_flush')
    result = seed.create_empty_thread(workspace, on_created=lambda _: None, **options)
    assert result['handoff_ready'] and (workspace / 'eof-seen').exists()
    assert Path(result['rollout_path']).exists()


@pytest.mark.parametrize('mode', ['missing', 'bad_header', 'bad_path', 'wrong_id', 'not_empty'])
def test_unpersisted_or_mismatched_empty_thread_is_never_handed_off(runtime, mode):
    workspace, options = runtime(mode)
    saved = []
    with pytest.raises(seed.DesktopSeedError) as error:
        seed.create_empty_thread(workspace, on_created=saved.append,
            persistence_timeout=.05, **options)
    assert error.value.code == 'seed_persistence_unconfirmed'
    assert error.value.thread_id == THREAD and saved == [THREAD]
    assert (workspace / 'eof-seen').exists()
    assert sum(item['method'] == 'thread/start' for item in requests(workspace)) == 1


@pytest.mark.parametrize('mode', ['name_failure', 'read_failure'])
def test_later_rpc_failure_keeps_the_already_saved_real_thread_identity(runtime, mode):
    workspace, options = runtime(mode)
    saved = []
    with pytest.raises(seed.DesktopSeedError) as error:
        seed.create_empty_thread(workspace, on_created=saved.append, title='Synthetic', **options)
    assert saved == [THREAD] and error.value.thread_id == THREAD
    assert error.value.code == 'seed_request_rejected'
    assert 'private' not in str(error.value) and 'private' not in str(error.value.details)
    assert (workspace / 'eof-seen').exists()


def test_callback_failure_occurs_before_any_followup_rpc_and_preserves_ack(runtime):
    workspace, options = runtime()
    def fails(_):
        raise RuntimeError('database unavailable')
    with pytest.raises(seed.DesktopSeedError) as error:
        seed.create_empty_thread(workspace, on_created=fails, title='Synthetic', **options)
    assert error.value.code == 'seed_identity_not_saved'
    assert error.value.thread_id == THREAD
    assert [r['method'] for r in requests(workspace)][-1] == 'thread/start'
    assert (workspace / 'eof-seen').exists()


def test_unknown_creation_is_not_retried_and_child_is_cleaned_up(runtime):
    workspace, options = runtime('start_timeout')
    server = seed.PlainAppServer(workspace, **options)
    server.__enter__()
    server.timeout = .1
    try:
        with pytest.raises(seed.DesktopSeedError) as error:
            server._create_empty(project_id=None, base_instructions=None, model=None,
                on_created=lambda _: pytest.fail('No acknowledgement was received'), title=None)
        assert error.value.code == 'seed_timeout' and error.value.outcome_unknown
        with pytest.raises(seed.DesktopSeedError, match='不能因结果未知'):
            server._create_empty(project_id=None, base_instructions=None, model=None,
                on_created=lambda _: None, title=None)
    finally:
        with pytest.raises(seed.DesktopSeedError):
            server.close()
    assert server.process_exited and server.process.poll() is not None
    assert not server.reader.is_alive()
    assert sum(r['method'] == 'thread/start' for r in requests(workspace)) == 1


def test_stalled_child_cannot_block_a_large_rpc_write_past_its_deadline(runtime):
    import time
    workspace, options = runtime('stop_reading', close_timeout=.1)
    server = seed.PlainAppServer(workspace, **options)
    server.__enter__()
    server.timeout = .1
    started = time.monotonic()
    try:
        with pytest.raises(seed.DesktopSeedError) as error:
            server.request('config/read', {'synthetic_padding': 'x' * 100000})
        assert error.value.code == 'seed_timeout'
        assert time.monotonic() - started < 2
    finally:
        with pytest.raises(seed.DesktopSeedError):
            server.close()
    assert server.process_exited and not server.reader.is_alive()


@pytest.mark.parametrize('mode,unknown', [('rejected', False), ('invalid_ack', True)])
def test_authoritative_rejection_and_invalid_ack_are_distinguished(runtime, mode, unknown):
    workspace, options = runtime(mode)
    saved = []
    with pytest.raises(seed.DesktopSeedError) as error:
        seed.create_empty_thread(workspace, on_created=saved.append, **options)
    assert error.value.outcome_unknown is unknown
    assert saved == []


@pytest.mark.parametrize('method', ['turn/start', 'turn/steer', 'thread/resume',
    'thread/fork', 'thread/stop', 'thread/unsubscribe', 'thread/start', 'thread/name/set'])
def test_public_rpc_cannot_start_models_or_take_thread_ownership(runtime, method):
    workspace, options = runtime()
    server = seed.PlainAppServer(workspace, **options)
    with pytest.raises(seed.DesktopSeedError) as error:
        server.request(method, {'threadId': 'some-other-thread'})
    assert error.value.code == 'seed_method_forbidden'
    assert server.process is None
    server.close()


def test_config_and_history_reads_never_resume_or_mutate_thread(runtime):
    workspace, options = runtime()
    with seed.PlainAppServer(workspace, **options) as server:
        assert server.request('config/read', {'cwd': str(workspace)})['config']['sandbox_mode'] == 'read-only'
        assert server.read_thread(THREAD)['thread']['id'] == THREAD
        assert server.read_turns(THREAD, limit=20)['data'] == []
    assert server.process_exited and not server.reader.is_alive()
    assert {r['method'] for r in requests(workspace)} == {
        'initialize', 'initialized', 'config/read', 'thread/read', 'thread/turns/list'}


def test_existing_project_registration_rpc_and_scoped_trust_write_are_supported(runtime):
    workspace, options = runtime()
    with seed.PlainAppServer(workspace, **options) as server:
        server.request('project/list', {})
        server.request('project/read', {'projectId': 'synthetic-project'})
        server.request('config/value/write', {
            'filePath': str(workspace / 'isolated-home' / 'config.toml'),
            'expectedVersion': 'checked-version', 'mergeStrategy': 'replace',
            'keyPath': 'projects.' + json.dumps(str(workspace), ensure_ascii=False) + '.trust_level',
            'value': 'trusted',
        })
        with pytest.raises(seed.DesktopSeedError) as error:
            server.request('config/value/write', {'keyPath': 'model', 'value': 'unrelated'})
        assert error.value.code == 'seed_config_scope'
    assert [r['method'] for r in requests(workspace)].count('config/value/write') == 1


def test_graceful_close_observes_eof_and_is_idempotent(runtime):
    workspace, options = runtime()
    server = seed.PlainAppServer(workspace, **options)
    server.__enter__()
    server.close()
    server.close()
    assert (workspace / 'eof-seen').exists()
    assert server.process_exited and not server.close_forced
    assert not server.reader.is_alive()


def test_timeout_only_terminates_the_owned_child_and_refuses_handoff(runtime):
    workspace, options = runtime('hang_eof', close_timeout=.05)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], creationflags=flags)
    try:
        server = seed.PlainAppServer(workspace, **options)
        server.__enter__()
        with pytest.raises(seed.DesktopSeedError) as error:
            server.close()
        assert error.value.code == 'seed_shutdown_incomplete'
        assert server.close_forced and server.process_exited
        assert not server.reader.is_alive()
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize('mode,code', [('oversized', 'seed_output_limit'),
    ('flood', 'seed_output_limit'), ('invalid_json', 'seed_protocol_error')])
def test_output_bounds_fail_without_reader_lock_or_child_leak(runtime, mode, code):
    workspace, options = runtime(mode, max_message_bytes=512, max_queue=4)
    server = seed.PlainAppServer(workspace, **options)
    try:
        server.__enter__()
        with pytest.raises(seed.DesktopSeedError) as error:
            server.request('config/read', {})
        assert error.value.code == code
    finally:
        try:
            server.close()
        except seed.DesktopSeedError:
            pass
    assert server.process_exited and not server.reader.is_alive()


def test_real_launch_argv_is_plain_app_server_without_legacy_isolation_flags(runtime, monkeypatch):
    workspace, fixture_options = runtime()
    seen = []
    def launch(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.Popen(fixture_options['command'], **kwargs)
    monkeypatch.setattr(seed, 'resolve_codex_cli', lambda: 'registered-current-cli.exe')
    with seed.PlainAppServer(workspace, popen_factory=launch,
                            environment=fixture_options['environment']) as server:
        server.request('config/read', {})
    assert seen[0][0] == ['registered-current-cli.exe', 'app-server']
    assert seen[0][1]['bufsize'] == 0
    if os.name == 'nt':
        assert seen[0][1]['creationflags'] == subprocess.CREATE_NO_WINDOW


def test_cli_resolution_uses_current_registration_not_leftover_version_mtime(tmp_path, monkeypatch):
    old = tmp_path / 'OpenAI.Codex_old' / 'app' / 'resources'
    current = tmp_path / 'OpenAI.Codex_current' / 'app' / 'resources'
    old.mkdir(parents=True)
    current.mkdir(parents=True)
    (old / 'codex.exe').write_bytes(b'old')
    (current / 'codex.exe').write_bytes(b'current')
    (current.parent / 'ChatGPT.exe').write_bytes(b'desktop')
    os.utime(old / 'codex.exe', (2_000_000_000, 2_000_000_000))
    installation = SimpleNamespace(install_location=str(current.parent.parent),
                                   executable=str(current.parent / 'ChatGPT.exe'))
    monkeypatch.setattr(seed.codex_installation, 'discover_codex', lambda: installation)
    assert seed.resolve_codex_cli() == str(current / 'codex.exe')
    (current / 'codex.exe').unlink()
    with pytest.raises(seed.DesktopSeedError) as error:
        seed.resolve_codex_cli()
    assert error.value.code == 'seed_cli_unavailable'


def test_missing_persistence_callback_never_spawns(runtime):
    workspace, options = runtime()
    with pytest.raises(ValueError):
        seed.create_empty_thread(workspace, **options)
    assert not (workspace / 'startup.json').exists()
