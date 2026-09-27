"""Synthetic regressions for the desktop handshake observed in Codex 26.924.

The startup network probe sends initialized; the persistent desktop client does
not. Neither path below starts a Codex process or submits a model request.
"""
import io
from types import SimpleNamespace

from management import codex_desktop_shim as shim
from test_codex_desktop_shim_v12 import Engine, relay


def _begin_desktop_initialize(value, desktop, request_id='__codex_initialize__'):
    value.endpoint = 'ws://127.0.0.1:12001'
    value.accept(desktop, {
        'id': request_id,
        'method': 'initialize',
        'params': {
            'clientInfo': {'name': 'codex_desktop', 'version': 'synthetic'},
            'capabilities': {'experimentalApi': True},
        },
    })
    return value.engine.sent[-1]['id']


def _success(value, request_id):
    result = {'userAgent': 'synthetic', 'platformFamily': 'windows'}
    value.receive_engine({'id': request_id, 'result': result})
    return result


def test_persistent_desktop_is_ready_without_initialized_notification(relay):
    value, engine, sent, published, desktop, manager = relay
    mapped = _begin_desktop_initialize(value, desktop)
    assert not published

    result = _success(value, mapped)

    assert published == [value.endpoint]
    assert sent[-1] == ('desktop', {'id': '__codex_initialize__', 'result': result})
    assert not any(message.get('method') == 'initialized' for message in engine.sent)

    before = list(engine.sent)
    value.accept(manager, {
        'id': 'management-initialize',
        'method': 'initialize',
        'params': {'capabilities': {'experimentalApi': True}},
    })
    assert sent[-1] == ('management', {'id': 'management-initialize', 'result': result})
    assert manager['initialized']
    assert engine.sent == before


def test_optional_initialized_notification_does_not_publish_twice(relay):
    value, engine, _, published, desktop, _ = relay
    _success(value, _begin_desktop_initialize(value, desktop, 'network-initialize'))
    assert published == [value.endpoint]

    value.accept(desktop, {'method': 'initialized'})
    value.accept(desktop, {'method': 'initialized', 'params': {}})
    value.publish_if_ready()

    assert published == [value.endpoint]
    assert engine.sent[-1] == {'method': 'initialized', 'params': {}}
    assert not engine.closed


def test_failed_desktop_initialize_never_publishes_ready(relay):
    value, _, sent, published, desktop, _ = relay
    mapped = _begin_desktop_initialize(value, desktop)
    error = {'code': -32000, 'message': 'Synthetic initialization failure'}
    value.receive_engine({'id': mapped, 'error': error})
    value.accept(desktop, {'method': 'initialized'})
    value.publish_if_ready()

    assert published == []
    assert value.initialize_result is None
    assert sent[-1] == ('desktop', {'id': '__codex_initialize__', 'error': error})


def test_management_initialize_cannot_publish_desktop_readiness(relay):
    value, engine, sent, published, _, manager = relay
    value.endpoint = 'ws://127.0.0.1:12001'
    value.accept(manager, {
        'id': 'management-initialize',
        'method': 'initialize',
        'params': {'capabilities': {'experimentalApi': True}},
    })
    value.publish_if_ready()

    assert published == []
    assert engine.sent == []
    assert not manager['initialized']
    assert sent[-1][1]['id'] == 'management-initialize'
    assert sent[-1][1]['error']['code'] == -32001


def test_initialized_notification_alone_does_not_publish_readiness(relay):
    value, _, _, published, desktop, _ = relay
    value.endpoint = 'ws://127.0.0.1:12001'
    value.accept(desktop, {'method': 'initialized'})
    value.publish_if_ready()

    assert published == []
    assert value.initialize_result is None


def test_main_handshake_replaces_probe_runtime_and_stale_cleanup_preserves_it(tmp_path):
    def make_relay(record):
        engine = Engine()
        value = shim._Relay(
            engine,
            SimpleNamespace(poll=lambda: None),
            SimpleNamespace(alive=lambda: True),
            'synthetic-token',
            lambda endpoint: shim._write_runtime(tmp_path, {**record, 'endpoint': endpoint}),
            io.BytesIO(),
            io.BytesIO(),
        )
        value.enqueue = lambda actor, message: None
        desktop = {
            'name': 'desktop', 'active': True, 'initialized': True,
            'queue': shim.queue.Queue(4), 'close': lambda: None,
        }
        value.actors['desktop'] = desktop
        return value, desktop

    probe_record = {
        'schema_version': 1, 'pid': 1001, 'engine_pid': 2001,
        'instance_id': 'synthetic-probe', 'ready': True,
        'token': 'synthetic-probe-token',
    }
    main_record = {
        'schema_version': 1, 'pid': 1002, 'engine_pid': 2002,
        'instance_id': 'synthetic-main', 'ready': True,
        'token': 'synthetic-main-token',
    }
    probe, probe_desktop = make_relay(probe_record)
    _success(probe, _begin_desktop_initialize(probe, probe_desktop, 'network-initialize'))
    probe.accept(probe_desktop, {'method': 'initialized'})
    assert shim._read_runtime(tmp_path) == {**probe_record, 'endpoint': probe.endpoint}
    probe.shutdown()

    main, main_desktop = make_relay(main_record)
    mapped = _begin_desktop_initialize(main, main_desktop)
    main.endpoint = 'ws://127.0.0.1:12002'
    _success(main, mapped)
    expected = {**main_record, 'endpoint': main.endpoint}
    assert shim._read_runtime(tmp_path) == expected

    shim._remove_runtime(tmp_path, probe_record)
    assert shim._read_runtime(tmp_path) == expected
    shim._remove_runtime(tmp_path, main_record)
    assert shim._read_runtime(tmp_path) is None
