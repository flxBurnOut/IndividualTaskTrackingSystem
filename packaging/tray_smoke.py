"""Native Windows tray acceptance using only an isolated empty data space.

Runs the frozen service/GUI and real Qt tray actions. Never controls another
application, uses personal data, or invokes Codex/model tasks. This verifies
native registration and lifecycle, not manual mouse/Explorer interaction.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil
from workflow import runtime_directory, require_test_workspace, checked_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from management import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, default=runtime_directory(__version__))
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve()
    assert os.name == 'nt' and package.is_relative_to(ROOT / 'release')
    service_exe, gui_exe = (package / name for name in ('PersonalManagementService.exe', 'PersonalManagement.exe'))
    assert service_exe.is_file() and gui_exe.is_file()
    for name in ('QT_QPA_PLATFORM', 'PERSONAL_MANAGEMENT_NO_TRAY'):
        os.environ.pop(name, None)
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    data = require_test_workspace(args.work_dir) / 'tray'
    assert data.is_relative_to(ROOT / '.build')
    data.mkdir()
    sentinel = data / 'synthetic-user-file.txt'
    sentinel.write_text('Keep this user file across close, restart and exit.', 'utf-8')
    report = {'version': __version__, 'data_dir': str(data), 'checks': {}, 'manual_mouse_test': False}
    owned = {}

    from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    from management.branding import configure_application
    from management.client import Client
    from management.gui import MainWindow
    from management.gui_gc import install_gui_gc
    from management.gui_instance import WindowInstance, window_running
    from management.gui_tray import ServiceTray
    from management.runtime import OwnerLock
    from management.tray_runtime import observer_running

    app = QApplication.instance() or QApplication([])
    configure_application(app)
    app.setQuitOnLastWindowClosed(False)
    manager = install_gui_gc(app)
    tray = window = instance = None
    tray_lock = OwnerLock(data / 'tray.lock')

    def wait(predicate, label, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            manager.poll()
            result = predicate()
            if result:
                return result
            time.sleep(.03)
        raise AssertionError('Timed out: ' + label)

    def register(process):
        owned[process.pid] = (process.create_time(), Path(process.exe()).resolve())
        return process

    def launch(executable, *arguments):
        child = subprocess.Popen([str(executable), '--data-dir', str(data), *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        register(psutil.Process(child.pid))
        return child

    def terminate_owned(pid):
        try:
            process = psutil.Process(pid)
            created, executable = owned[pid]
            assert process.create_time() == created and Path(process.exe()).resolve() == executable
            command = process.cmdline()
            assert '--data-dir' in command and Path(command[command.index('--data-dir') + 1]).resolve() == data
            process.terminate()
            process.wait(10)
        except psutil.NoSuchProcess:
            pass

    def observation(exclude_pid=None):
        try:
            value = json.loads((data / 'tray-status.json').read_text('utf-8'))
            assert Path(value['data_dir']).resolve() == data and value['version'] == __version__
            if value['pid'] == exclude_pid or '后台运行中' not in value['status']:
                return None
            if time.time() - value['checked_at'] > 6:
                return None
            process = psutil.Process(value['pid'])
            assert Path(process.exe()).resolve() == gui_exe and '--tray' in process.cmdline()
            register(process)
            return value if value['icon_registered'] and value['system_tray_available'] else None
        except (OSError, ValueError, psutil.NoSuchProcess):
            return None

    def gui_processes():
        found = []
        for process in psutil.process_iter(['pid', 'exe', 'cmdline']):
            info = process.info
            command = info['cmdline'] or []
            if not info['exe'] or Path(info['exe']).resolve() != gui_exe or '--tray' in command:
                continue
            if '--data-dir' in command and Path(command[command.index('--data-dir') + 1]).resolve() == data:
                found.append(register(process))
        return found

    try:
        service = launch(service_exe, '--service')
        wait(lambda: (data / 'runtime.json').exists(), 'service discovery')
        client = Client(data, autostart=False)
        before = client.state()
        initial = wait(observation, 'service-created native tray')
        assert QSystemTrayIcon.isSystemTrayAvailable() and observer_running(data)
        report['native_geometry'] = initial['geometry']
        report['checks']['service_creates_native_tray'] = True

        duplicate = launch(gui_exe, '--tray')
        wait(lambda: duplicate.poll() is not None, 'duplicate tray exits')
        assert duplicate.returncode == 0 and observation()['pid'] == initial['pid']
        report['checks']['single_tray_per_data_space'] = True

        terminate_owned(initial['pid'])
        restored = wait(lambda: observation(initial['pid']), 'observer supervised recovery')
        report['checks']['crashed_observer_recreated'] = True

        instance = WindowInstance(data, app)
        assert instance.acquire()
        window = MainWindow(data)
        window.setWindowTitle('托盘验收（隔离空数据）')
        requests = []
        instance.requested.connect(requests.append)
        instance.requested.connect(window.activate_from_tray)
        window.show()
        wait(lambda: window.type_map and not window.bridge.callbacks, 'main window loaded')
        duplicate = launch(gui_exe)
        wait(lambda: duplicate.poll() is not None and requests == ['show'], 'reuse existing main window')
        assert duplicate.returncode == 0
        window.close()
        wait(lambda: not window.isVisible(), 'normal main-window close')
        instance.close()
        instance = None
        assert service.poll() is None and observation()['pid'] == restored['pid']
        report['checks']['closing_window_keeps_service_and_tray'] = True
        report['checks']['shortcut_reuses_window'] = True

        # Take over only this synthetic observer lock to exercise actual Qt
        # QAction slots against the frozen service and GUI, without mouse input.
        terminate_owned(restored['pid'])
        assert tray_lock.acquire()
        tray = ServiceTray(data, app, launcher=lambda root, show_update=False:
            launch(gui_exe, *(['--show-update'] if show_update else [])))
        wait(lambda: '后台运行中' in tray.status.text(), 'interactive tray status')
        tray.open_action.trigger()
        wait(lambda: window_running(data) and len(gui_processes()) == 1, 'tray opens frozen GUI')
        first_pid = gui_processes()[0].pid
        tray.open_action.trigger()
        wait(lambda: not tray.pending, 'second open action settled')
        assert [process.pid for process in gui_processes()] == [first_pid]
        report['checks']['tray_opens_and_reuses_frozen_gui'] = True
        tray.menu.setWindowTitle('个人事务管理 · 托盘菜单验收')
        tray.menu.ensurePolished()
        tray.menu.resize(tray.menu.sizeHint())
        assert tray.menu.grab().save(str(data / 'tray-menu.png'))
        wait(lambda: not tray.pending, 'status request settled')
        tray.exit_action.trigger()
        # An in-flight initial GUI read can legitimately defer exit; wait for
        # that explicit refusal before one additional user-equivalent action.
        try:
            wait(lambda: service.poll() is not None, 'GUI-guarded tray exit', seconds=7)
        except AssertionError:
            tray.exit_action.trigger()
            wait(lambda: service.poll() is not None, 'idle GUI-guarded tray exit')
        wait(lambda: tray.closing and not gui_processes(), 'GUI and tray shutdown')
        assert service.returncode == 0 and not tray.icon.isVisible()
        marker = json.loads((data / 'update_pending.json').read_text('utf-8'))
        assert marker['reason'] == 'exit' and marker['status'] == 'ready'
        assert sentinel.read_text('utf-8').startswith('Keep this user file')
        report['checks']['exit_stops_gui_service_and_tray'] = True
        report['checks']['explicit_exit_pauses_automatic_restart'] = True
        report['checks']['user_file_preserved'] = True
        report['initial_epoch'] = before['epoch']
        report['passed'] = all(report['checks'].values())
    finally:
        if tray:
            tray.shutdown()
            tray.menu.close()
        if window and window.isVisible():
            window.close()
        if instance:
            instance.close()
        tray_lock.release()
        for pid in owned:
            terminate_owned(pid)
        wait(manager.shutdown, 'Qt worker shutdown')
        report['owned_processes_stopped'] = True
        path = checked_path(args.report, ROOT / '.build' / 'checks')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
