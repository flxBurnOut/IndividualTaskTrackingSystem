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
