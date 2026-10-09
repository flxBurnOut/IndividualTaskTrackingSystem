from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .runtime import default_data_dir
from .data_space import BetaIsolationError, require_beta_dir
from .branding import APP_NAME


def main():
    parser = argparse.ArgumentParser(description=APP_NAME + ' · 原生界面与 Codex 共用独立 Beta 业务服务')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--verify-ui', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--choose-data', action='store_true')
    parser.add_argument('--launch-codex', action='store_true', help='兼容入口：打开 Beta 软件并自动准备已启用的 Codex 连接')
    parser.add_argument('--install-shortcuts', action='store_true', help='为所选 Beta 数据空间创建桌面与开始菜单快捷方式')
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--bootstrap-service', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--resume-update', help=argparse.SUPPRESS)
    parser.add_argument('--tray', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--show-update', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--mcp', action='store_true')
    parser.add_argument('--mcp-discussion')
    parser.add_argument('--discussion-epoch')
    parser.add_argument('--diagnose', action='store_true')
    parser.add_argument('--seed-demo', action='store_true', help='在空 Beta 空间生成虚构演示数据；重复运行保留现有编辑')
    parser.add_argument('--demo-date', help='演示基准日期 YYYY-MM-DD；默认首次生成当天，续跑保留原日期')
    parser.add_argument('--source-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--document-worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source_worker:
        from .source_worker import worker
        return worker(args.source_worker)
    if args.document_worker:
        from .document_worker import worker
        return worker(args.document_worker)
    try:
        args.data_dir = require_beta_dir(args.data_dir if args.data_dir is not None else default_data_dir())
    except BetaIsolationError as error:
        parser.error(str(error))
    if args.demo_date and not args.seed_demo:
        parser.error('--demo-date 必须与 --seed-demo 一起使用')
    if args.seed_demo:
        from .client import Client
        from .demo_data import seed_demo
        from .installation_state import resume_after_update
        try:
            resume_after_update(args.data_dir)
            result = seed_demo(Client(args.data_dir), args.data_dir, reference_date=args.demo_date)
        except Exception as error:
            parser.exit(1, str(error) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.bootstrap_service:
        from .runtime import bootstrap_service
        bootstrap_service(args.data_dir, resume_token=args.resume_update)
        return 0
    if args.service:
        from .service import run_service
        return run_service(args.data_dir, resume_token=args.resume_update)
    if args.mcp_discussion:
        from .discussion_mcp import create_server
        return create_server(args.data_dir,args.mcp_discussion,args.discussion_epoch).run(transport='stdio')
    if args.mcp:
        from .mcp_server import run
        return run(args.data_dir)
    if args.diagnose:
        from .client import Client
        print(json.dumps(Client(args.data_dir).query('diagnostics'), ensure_ascii=False, indent=2))
        return 0
    if args.verify_ui:
        from .verify_ui import verify
        return verify(args.data_dir, args.verify_ui)
    if args.tray:
        from .gui_tray import run
        return run(args.data_dir)
    if args.choose_data:
        from .launcher import choose_data_dir
        selected = choose_data_dir()
        if selected is None:
            return 0
        args.data_dir = selected
        try:
            args.data_dir = require_beta_dir(args.data_dir)
        except BetaIsolationError as error:
            parser.error(str(error))
    if args.install_shortcuts:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from .branding import set_windows_identity, configure_application
        from .shortcuts import install_shortcuts
        set_windows_identity()
        app = QApplication.instance() or QApplication([])
        configure_application(app)
        try:
            result = install_shortcuts(args.data_dir)
        except Exception as error:
            QMessageBox.warning(None, '创建快捷方式', str(error))
            return 1
        QMessageBox.information(None, 'Beta 快捷方式已创建', f'桌面和开始菜单中都可以找到“{APP_NAME}”。\n\n打开的 Beta 数据空间：'+result['data_dir'])
        return 0
    from .installation_state import resume_after_update
    try:
        resume_after_update(args.data_dir)
    except (ValueError, OSError) as error:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.warning(None, '更新尚未完成', str(error))
        return 1
    from .gui import run
    return run(args.data_dir, show_update=args.show_update)


if __name__ == '__main__':
    sys.exit(main())
