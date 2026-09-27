"""Opt-in Windows acceptance using tiny payloads and a unique installer identity.

Run with PERSONAL_MANAGEMENT_INSTALLER_TEST=1.  No product executable is run,
no real installation is replaced, and no user data is opened.  The fixture's
uninstaller removes only its own files and registration at the end.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / ".build/tools/inno-setup-7.1.0/compiler/ISCC.exe"
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("PERSONAL_MANAGEMENT_INSTALLER_TEST") != "1" or not COMPILER.is_file(),
    reason="Requires opt-in Windows installer acceptance and the pinned local compiler",
)


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def data_space(path):
    path.mkdir(parents=True)
    with sqlite3.connect(path / "database.sqlite3") as database:
        database.execute("create table personal_notes (id integer primary key, body text)")
        database.execute("insert into personal_notes values (1, '合成资料：保留原文')")
    (path / "materials").mkdir()
    (path / "materials/原始附件.bin").write_bytes(bytes(range(256)))
    (path / "preferences.json").write_text('{"custom": "保留设置"}', encoding="utf-8")
    return path


class InstallerFixture:
    def __init__(self, root):
        import winreg
        self.root = root
        self.app = root / "program"
        self.name = "PersonalManagement-Installer-Test-" + uuid.uuid4().hex
        self.app_id = "{" + str(uuid.uuid4()).upper() + "}"
        self.key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + self.app_id + "_is1"
        self.view = winreg.KEY_WOW64_64KEY
        self.shortcuts = []
        self.installers = {}
        self.sequence = 0
        for version in ("0.0.1", "0.0.2"):
            package = root / ("payload-" + version)
            icons = package / "_internal/management/assets"
            icons.mkdir(parents=True)
            shutil.copyfile(ROOT / "src/management/assets/app-icon.ico", icons / "app-icon.ico")
            for name in ("PersonalManagement.exe", "PersonalManagementService.exe", "PersonalManagementCodex.exe"):
                # Setup only copies these marker files; the test never launches them.
                (package / name).write_text("synthetic program " + version, encoding="ascii")
            (package / "详细使用说明.md").write_text("synthetic documentation", encoding="ascii")
            command = [str(COMPILER), "/Qp", "/DAppVersion=" + version,
                       "/DPackageDir=" + str(package), "/DOutputDir=" + str(root),
                       "/DAppIdentity=" + self.app_id, "/DAppName=" + self.name,
                       "/DDataDirectory=" + str(root / "unused-default-data"),
                       str(ROOT / "packaging/installer.iss")]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            assert result.returncode == 0, result.stdout + result.stderr
            self.installers[version] = root / ("PersonalManagement-" + version + "-Setup-x64.exe")

    def values(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_READ | self.view) as key:
                result = {}
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, index)
                    result[name] = value
                return result
        except FileNotFoundError:
            return {}

    def forget_data_directory(self):
        import winreg
        values = self.values()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE | self.view) as key:
            for name in values:
                if name == "DataDirectory" or name.startswith("Inno Setup CodeFile:") and name.endswith("DataDirectory"):
                    winreg.DeleteValue(key, name)

    def remember_data_directory(self, path):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE | self.view) as key:
            winreg.SetValueEx(key, "DataDirectory", 0, winreg.REG_SZ, str(path))

    def install(self, version, *, data=None, app=None):
        self.sequence += 1
        log = self.root / ("setup-" + str(self.sequence) + ".log")
        command = [str(self.installers[version]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                   "/NOICONS", "/TASKS=", "/DIR=" + str(app or self.app), "/LOG=" + str(log)]
        if data is not None:
            command.append("/DATADIR=" + str(data))
        result = subprocess.run(command, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        raw = log.read_bytes()
        self.last_log = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
        return result.returncode

    def shortcut(self, data, *, desktop=False):
        from win32com.shell import shell
        from management.shortcuts import write_shortcut
        folder = Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Desktop if desktop else shell.FOLDERID_Programs))
        if not desktop:
            folder /= self.name
        path = folder / (self.name + ".lnk")
        assert not path.exists(), "Unique synthetic shortcut unexpectedly exists"
        self.shortcuts.append((path, not desktop))
        write_shortcut(path, self.app / "PersonalManagement.exe", data)

    def uninstall(self):
        if not self.values():
            return
        uninstallers = list(self.app.glob("unins*.exe"))
        if uninstallers:
            assert len(uninstallers) == 1
            result = subprocess.run([str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                     "/LOG=" + str(self.root / "uninstall.log")], timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            assert result.returncode == 0
        assert not self.values(), "Synthetic installer registration survived uninstall"

    def cleanup(self):
        self.uninstall()
        for path, remove_parent in self.shortcuts:
            path.unlink(missing_ok=True)
            if remove_parent and path.parent.exists():
                # The unique fixture directory must contain no other files.
                path.parent.rmdir()


@pytest.fixture
def setup(tmp_path):
    fixture = InstallerFixture(tmp_path)
    assert not fixture.values()
    try:
        yield fixture
    finally:
        fixture.cleanup()


def test_upgrade_replaces_program_preserves_custom_space_and_uninstall(setup):
    data = data_space(setup.root / "自选资料 有空格")
    before = hashes(data)
    assert setup.install("0.0.1", data=data) == 0, setup.last_log
    assert Path(setup.values()["DataDirectory"]) == data
    personal_file = setup.app / "personal-file.txt"
    personal_file.write_text("user-owned file survives", encoding="ascii")
    assert setup.install("0.0.2") == 0, setup.last_log
    assert setup.values()["DisplayVersion"] == "0.0.2"
    for missing in (setup.root / "unmounted-data", setup.root / "empty-data"):
        if missing.name == "empty-data":
            missing.mkdir()
        assert setup.install("0.0.2", data=missing) != 0
        assert "安装器不会新建空库来替代原资料" in setup.last_log
        assert Path(setup.values()["DataDirectory"]) == data
    assert Path(setup.values()["DataDirectory"]) == data
    assert (setup.app / "PersonalManagement.exe").read_text("ascii") == "synthetic program 0.0.2"
    assert hashes(data) == before
    assert not (setup.root / "unused-default-data").exists()
    assert setup.install("0.0.1") != 0
    assert "不能直接降级" in setup.last_log
    assert setup.values()["DisplayVersion"] == "0.0.2"
    assert setup.install("0.0.2", data=data, app=setup.root / "second-program") != 0
    assert "升级需要替换已登记的旧程序" in setup.last_log
    setup.uninstall()
    assert not (setup.app / "PersonalManagement.exe").exists()
    assert personal_file.read_text("ascii") == "user-owned file survives"
    assert hashes(data) == before


def test_legacy_shortcuts_are_retained_and_ambiguity_never_chooses_empty_data(setup):
    first = data_space(setup.root / "原数据")
    second = data_space(setup.root / "另一个空间")
    before = {str(root): hashes(root) for root in (first, second)}
    assert setup.install("0.0.1", data=first) == 0, setup.last_log
    setup.forget_data_directory()
    assert setup.install("0.0.2") != 0
    assert "无法确定旧版使用的数据目录" in setup.last_log
    assert setup.values()["DisplayVersion"] == "0.0.1"
    setup.shortcut(first)
    assert setup.install("0.0.2") == 0, setup.last_log
    assert Path(setup.values()["DataDirectory"]) == first
    setup.shortcut(second, desktop=True)
    # A new installation's stored selection takes precedence over stale links.
    assert setup.install("0.0.2") == 0, setup.last_log
    assert Path(setup.values()["DataDirectory"]) == first
    setup.remember_data_directory(setup.root / "missing-disk")
    assert setup.install("0.0.2") != 0
    assert "安装器不会新建空库来替代原资料" in setup.last_log
    setup.forget_data_directory()
    assert setup.install("0.0.2") != 0
    assert "无法确定旧版使用的数据目录" in setup.last_log
    assert setup.install("0.0.2", data=second) == 0, setup.last_log
    assert Path(setup.values()["DataDirectory"]) == second
    assert {str(root): hashes(root) for root in (first, second)} == before


def test_program_and_data_boundaries_block_before_installing(setup):
    data = data_space(setup.root / "资料")
    before = hashes(data)
    unknown = setup.root / "unrelated-program"
    unknown.mkdir()
    (unknown / "personal.txt").write_text("preserve", encoding="ascii")
    assert setup.install("0.0.1", data=data, app=unknown) != 0
    assert "所选目录已有其他文件" in setup.last_log
    assert setup.install("0.0.1", data=data, app=data) != 0
    assert setup.install("0.0.1", data=data, app=data / "program") != 0
    nested = data_space(setup.app / "data")
    assert setup.install("0.0.1", data=nested) != 0
    assert setup.install("0.0.1", data=unknown, app=setup.root / "empty-program") != 0
    assert "所选数据目录已有其他文件" in setup.last_log
    assert not setup.values()
    assert hashes(data) == before
    assert (unknown / "personal.txt").read_text("ascii") == "preserve"
    assert not (setup.app / "PersonalManagement.exe").exists()


def test_nested_program_junction_cannot_redirect_upgrade_into_personal_data(setup):
    import _winapi
    data = data_space(setup.root / "资料")
    before = hashes(data)
    assert setup.install("0.0.1", data=data) == 0, setup.last_log
    junction = setup.app / "_internal/linked-personal-data"
    _winapi.CreateJunction(str(data), str(junction))
    try:
        assert junction.is_junction()
        assert setup.install("0.0.2") != 0
        assert "程序目录内存在链接文件或链接目录" in setup.last_log
        assert setup.values()["DisplayVersion"] == "0.0.1"
        assert hashes(data) == before
    finally:
        # Remove only the fixture-created junction, never its target tree.
        assert junction.parent.resolve() == (setup.app / "_internal").resolve()
        assert junction.is_junction() and junction.resolve() == data.resolve()
        junction.rmdir()
