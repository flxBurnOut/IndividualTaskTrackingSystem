"""Installer payload must never carry a user's upgrade state or backup."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer_build_for_test", ROOT / "packaging/build_installer.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


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
    "_internal/upgrade-backups/personal-attachment.txt",
])
def test_user_upgrade_state_is_rejected_before_installer_compilation(tmp_path, monkeypatch, private_path):
    monkeypatch.setattr(BUILD, "ROOT", tmp_path)
    monkeypatch.setattr(BUILD, "pe_version", lambda path: "1.1.0.0")
    package = tmp_path / "release/PersonalManagement-1.1.0"
    for name in (*BUILD.EXES, "_internal/management/assets/app-icon.ico", "使用说明.txt", "详细使用说明.md"):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic release input")
    assert BUILD.validate_package(package, "1.1.0")
    private = package / private_path
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"synthetic private upgrade state")
    with pytest.raises(ValueError, match="Private/runtime data"):
        BUILD.validate_package(package, "1.1.0")


def test_check_cannot_remove_existing_release_installer(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
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
