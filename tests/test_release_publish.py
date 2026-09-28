"""A release must contain exactly the reviewed, hash-matched public assets."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('release_publish_test', Path(__file__).resolve().parents[1] / 'packaging/publish.py')
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)


def test_publish_rejects_a_repository_other_than_origin(monkeypatch):
    monkeypatch.setattr(PUBLISH, 'git', lambda *args: 'https://github.com/example/authorized.git')
    with pytest.raises(ValueError, match='differs from origin'):
        PUBLISH.configure('1.1.1', 'another/destination')


@pytest.mark.parametrize('change', ['extra', 'digest', 'size', 'state'])
def test_publication_rejects_extra_or_changed_assets(change):
    expected = {'installer.exe': {'bytes': 123, 'sha256': 'abc'}}
    asset = {'name': 'installer.exe', 'size': 123, 'digest': 'sha256:abc',
             'state': 'uploaded', 'browser_download_url': 'https://example.invalid/installer.exe'}
    assets = [asset]
    if change == 'extra':
        assets.append({**asset, 'name': 'private-data.json'})
    else:
        asset[{'size': 'size', 'digest': 'digest', 'state': 'state'}[change]] = 'changed'
    with pytest.raises(RuntimeError):
        PUBLISH.verify_assets({'assets': assets}, expected)


def test_publication_accepts_only_the_exact_uploaded_assets():
    expected = {'installer.exe': {'bytes': 123, 'sha256': 'abc'}}
    asset = {'name': 'installer.exe', 'size': 123, 'digest': 'sha256:abc',
             'state': 'uploaded', 'browser_download_url': 'https://example.invalid/installer.exe'}
    assert PUBLISH.verify_assets({'assets': [asset]}, expected)['installer.exe']['digest'] == 'sha256:abc'
