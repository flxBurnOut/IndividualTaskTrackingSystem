"""Installer payload must never carry a user's upgrade state or backup."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer_build_for_test", ROOT / "packaging/build_installer.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


def synthetic_package(tmp_path, monkeypatch):
    monkeypatch.setattr(BUILD, 'ROOT', tmp_path)
    monkeypatch.setattr(BUILD, 'pe_version', lambda path: '1.1.0.0')
    package = tmp_path / 'release/PersonalManagement-1.1.0'
    for name in (*BUILD.EXES, '_internal/management/assets/app-icon.ico', '使用说明.txt', '详细使用说明.md'):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic release input')
    policy = BUILD.ssl_policy_file()
    if policy is not None:
        (package / BUILD.SSL_POLICY_MEMBER).write_bytes(policy.read_bytes())
    return package


@pytest.mark.parametrize("private_path", [
    "upgrade-backups/backup-manifest.json",
    "upgrade-state.json",
    "schema-upgrade.lock",
    "update_pending.json",
    "update_pending.json.new",
    "startup_failure.json",
    "startup_failure.json.new",
    "gui.lock",
    "tray.lock",
    "tray-status.json",
    "tray-status.json.new",
    "ui-onboarding.json",
    "ui-plan-drafts/synthetic-draft.json",
    "_internal/upgrade-backups/personal-attachment.txt",
])
def test_user_upgrade_state_is_rejected_before_installer_compilation(tmp_path, monkeypatch, private_path):
    package = synthetic_package(tmp_path, monkeypatch)
    assert BUILD.validate_package(package, "1.1.0")
    private = package / private_path
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"synthetic private upgrade state")
    with pytest.raises(ValueError, match="Private/runtime data"):
        BUILD.validate_package(package, "1.1.0")


def test_check_cannot_remove_existing_release_installer(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from management import data_space
    # Exercise the legacy compiler path on a synthetic package only. Actual
    # Beta invocation is separately checked to stop before reaching this path.
    monkeypatch.setattr(data_space, 'require_packaged_channel', lambda: None)
    monkeypatch.setattr(BUILD, 'ROOT', tmp_path)
    monkeypatch.setattr(BUILD, 'source_version', lambda: '1.1.1')
    monkeypatch.setattr(BUILD, 'compiler_path', lambda *args: (tmp_path / 'ISCC.exe', '7.1.0'))
    monkeypatch.setattr(BUILD, 'validate_package', lambda *args: [])
    monkeypatch.setattr(BUILD, 'digest', lambda *args: 'synthetic')
    output = tmp_path / 'release/PersonalManagement-1.1.1/PersonalManagement-1.1.1-Setup-x64.exe'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'verified installer')
    def compiler(command, **kwargs):
        destination = Path(next(arg.removeprefix('/DOutputDir=') for arg in command if arg.startswith('/DOutputDir=')))
        assert destination.is_relative_to(tmp_path / '.build')
        # Model a compiler cleaning its previous output during a dry compile.
        (destination / output.name).unlink(missing_ok=True)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(BUILD.subprocess, 'run', compiler)
    monkeypatch.setattr(sys, 'argv', ['build_installer.py', '--check'])
    BUILD.main()
    assert output.read_bytes() == b'verified installer'


@pytest.mark.parametrize('module_name', ['build', 'build_installer', 'publish'])
def test_beta_blocks_formal_packaging_before_any_external_action(monkeypatch, module_name):
    import importlib
    import sys
    from management.data_space import BetaIsolationError
    monkeypatch.setattr('management.data_space.CHANNEL', 'beta')
    monkeypatch.syspath_prepend(str(ROOT / 'packaging'))
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('Executed formal packaging'))
    monkeypatch.setattr(sys, 'argv', [module_name + '.py'])
    with pytest.raises(BetaIsolationError, match='Beta'):
        module.main()


@pytest.mark.parametrize('member', ['_internal/unrelated.pem', '_internal/private.key', '_internal/codex-rejected-cas.pem'])
def test_certificate_allowance_cannot_include_private_or_substituted_data(tmp_path, monkeypatch, member):
    package = synthetic_package(tmp_path, monkeypatch)
    target = package / member
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'-----BEGIN PRIVATE KEY-----\nsynthetic private material\n')
    with pytest.raises(ValueError, match='Private/runtime file'):
        BUILD.validate_package(package, '1.1.0')


def test_runtime_policy_is_required_when_the_selected_ssl_module_uses_it(tmp_path, monkeypatch):
    import runtime_resources as resources
    from types import SimpleNamespace
    stdlib = tmp_path / 'stdlib'
    stdlib.mkdir()
    source = stdlib / 'ssl.py'
    source.write_text('# runtime requires ' + resources.SSL_POLICY_NAME, encoding='utf-8')
    monkeypatch.setattr(resources, 'ssl', SimpleNamespace(__file__=str(source)))
    with pytest.raises(ValueError, match='missing its public SSL policy'):
        resources.runtime_data_files()
    policy = stdlib / resources.SSL_POLICY_NAME
    policy.write_bytes(b'-----BEGIN TRUSTED CERTIFICATE-----\nsynthetic public certificate\n')
    assert resources.runtime_data_files() == [(str(policy), '.')]
    package = synthetic_package(tmp_path, monkeypatch)
    assert BUILD.validate_package(package, '1.1.0')
    (package / resources.SSL_POLICY_MEMBER).unlink()
    with pytest.raises(ValueError, match='Missing release input'):
        BUILD.validate_package(package, '1.1.0')
    policy.write_bytes(b'-----BEGIN PRIVATE KEY-----\nsynthetic\n')
    with pytest.raises(ValueError, match='public trusted-certificate data only'):
        resources.runtime_data_files()


def test_standard_python_requires_no_added_certificate_policy(tmp_path, monkeypatch):
    import runtime_resources as resources
    from types import SimpleNamespace
    source = tmp_path / 'ssl.py'
    source.write_text('# unmodified SSL module', encoding='utf-8')
    monkeypatch.setattr(resources, 'ssl', SimpleNamespace(__file__=str(source)))
    assert resources.runtime_data_files() == []


def test_ssl_initialization_keeps_runtime_policy_after_relocation(tmp_path, monkeypatch):
    import ssl
    import sys
    import runtime_resources as resources
    source = Path(ssl.__file__)
    relocated = tmp_path / 'ssl.py'
    relocated.write_bytes(source.read_bytes())
    spec = importlib.util.spec_from_file_location('relocated_runtime_ssl', relocated)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    policy = resources.ssl_policy_file()
    if policy is not None:
        with pytest.raises(FileNotFoundError):
            module.create_default_context()
        for original, destination in resources.runtime_data_files():
            target = tmp_path / destination / Path(original).name
            target.write_bytes(Path(original).read_bytes())
        assert (tmp_path / policy.name).read_bytes() == policy.read_bytes()
    context = module.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
