"""Real child-process/Windows-pipe exit checks, without Codex or user data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading

import pytest

from management import codex_desktop_shim as shim


_CHILD = textwrap.dedent(r'''
    import json
    import sys
    import threading
    from types import SimpleNamespace
    from websockets.sync.client import connect
    from websockets.sync.server import serve
    from management import codex_desktop_shim as shim

    # Real engine transport closes while the parent still owns an open stdin
    # pipe. No model or user Codex process participates in this check.
    def engine_handler(connection):
        threading.Event().wait(.35)
        connection.close(code=1001, reason='isolated shutdown check')

    with serve(engine_handler, '127.0.0.1', 0) as server:
        listener = threading.Thread(target=server.serve_forever, daemon=True)
        listener.start()
        endpoint = 'ws://127.0.0.1:' + str(server.socket.getsockname()[1])
        with connect(endpoint, proxy=None) as engine:
            relay = shim._Relay(engine, SimpleNamespace(poll=lambda: None),
                SimpleNamespace(alive=lambda: True), 'test-only-' * 8,
                lambda endpoint: None, sys.stdin.buffer, sys.stdout.buffer)
            try:
                relay.run()
            except shim.ShimError:
                pass
        server.shutdown()
        listener.join(timeout=2)
    print(json.dumps({'stopped': relay.stop.is_set(), 'input_alive': any(
        item.name == 'codex-desktop-input' and item.is_alive()
        for item in threading.enumerate())}), flush=True)
''')


@pytest.mark.skipif(os.name != 'nt', reason='Windows anonymous-pipe shutdown regression')
@pytest.mark.parametrize('partial', [b'', b'{"id":1,"method":"initialize"'])
def test_engine_disconnect_exits_with_desktop_stdin_still_open(partial):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    process = subprocess.Popen([sys.executable, '-u', '-c', _CHILD],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        if partial:
            process.stdin.write(partial)
            process.stdin.flush()
        # Do not use communicate(): it closes stdin and masks the defect.
        process.wait(timeout=8)
        output = process.stdout.read().decode('utf-8', 'replace')
        errors = process.stderr.read().decode('utf-8', 'replace')
        assert process.returncode == 0, errors
        assert '_enter_buffered_busy' not in errors
        assert 'Fatal Python error' not in errors
        assert json.loads(output) == {'stopped': True, 'input_alive': False}
    finally:
        if process.poll() is None:
            # This is only the isolated child created by this test.
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows pipe reader')
@pytest.mark.parametrize('payload,expected', [
    ('{"text":"事务"}\r\n{}\n'.encode('utf-8'),
     ['{"text":"事务"}\r\n'.encode('utf-8'), b'{}\n', b'']),
    (b'{"unfinished":', [b'{"unfinished":']),
    (b'', [b'']),
])
def test_real_pipe_preserves_frames_and_eof(payload, expected):
    read_descriptor, write_descriptor = os.pipe()
    with os.fdopen(read_descriptor, 'rb') as stream:
        try:
            if payload:
                os.write(write_descriptor, payload)
        finally:
            os.close(write_descriptor)
        assert list(shim._desktop_lines(stream, threading.Event())) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows pipe reader')
def test_real_pipe_keeps_existing_frame_read_bound(monkeypatch):
    monkeypatch.setattr(shim, 'MAX_FRAME', 16)
    read_descriptor, write_descriptor = os.pipe()
    with os.fdopen(read_descriptor, 'rb') as stream:
        try:
            os.write(write_descriptor, b'x' * 30)
            lines = shim._desktop_lines(stream, threading.Event())
            assert next(lines) == b'x' * 18
        finally:
            os.close(write_descriptor)
