"""License downloads may overlap; guarded package writes must not."""
import hashlib
import importlib.util
import json
from pathlib import Path
import threading

import pytest


@pytest.fixture
def notices():
    path = Path(__file__).resolve().parents[1] / 'packaging' / 'license_notices.py'
    spec = importlib.util.spec_from_file_location('license_notices_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parallel_downloads_are_written_on_caller_thread(notices, tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    barrier = threading.Barrier(2)
    files = {
        'src/3rdparty/example/LICENSE.txt': b'Synthetic license\n',
        'src/3rdparty/example/qt_attribution.json': b'{"Name":"Synthetic"}\n',
    }
    tree = {'sha': notices.QT_SOURCES['qtbase'], 'tree': [
        {'type': 'blob', 'path': path, 'sha': 'synthetic-blob'} for path in files
    ]}
    fetch_threads = []

    def fetch(url, **checks):
        if 'api.github.com' in url:
            return json.dumps(tree).encode()
        for path, content in files.items():
            if url.endswith('/' + path):
                fetch_threads.append(threading.get_ident())
                barrier.wait(timeout=5)
                assert checks == {'git_blob': 'synthetic-blob'}
                return content
        return b'Synthetic upstream terms\n'

    original_put = notices._put

    def put(folder, relative, content):
        # Regression: workers formerly created shared parent directories while
        # another worker resolved a not-yet-existing destination on Windows.
        assert threading.get_ident() == main_thread
        return original_put(folder, relative, content)

    monkeypatch.setattr(notices, '_fetch', fetch)
    monkeypatch.setattr(notices, '_put', put)
    result = notices._qt_notices(tmp_path, ['Qt6Core.dll'], {'PySide6': notices.QT_VERSION})
    assert result['supplemental_files'] == len(files)
    assert len(set(fetch_threads)) == 2 and main_thread not in fetch_threads
    root = tmp_path / ('Qt-' + notices.QT_VERSION)
    records = json.loads((root / 'UPSTREAM_FILES.json').read_text())
    assert len(records) == len(files)
    for record, (path, content) in zip(records, files.items()):
        assert record['file'] == 'Qt-' + notices.QT_VERSION + '/qtbase/' + path
        assert record['sha256'] == hashlib.sha256(content).hexdigest()
        assert (tmp_path / record['file']).read_bytes() == content


@pytest.mark.parametrize('relative', ['../escaped.txt', '/escaped.txt', 'C:/escaped.txt'])
def test_notice_destination_rejects_unsafe_path(notices, tmp_path, relative):
    with pytest.raises(RuntimeError, match='Invalid notice destination'):
        notices._put(tmp_path, relative, b'Synthetic notice')
    assert list(tmp_path.iterdir()) == []


def test_notice_destination_rejects_link_outside_package(notices, tmp_path):
    folder, outside = tmp_path / 'package', tmp_path / 'outside'
    folder.mkdir()
    outside.mkdir()
    try:
        (folder / 'redirect').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This test host does not allow directory symlinks')
    with pytest.raises(RuntimeError, match='Notice destination escaped'):
        notices._put(folder, 'redirect/LICENSE.txt', b'Synthetic notice')
    assert list(outside.iterdir()) == []
