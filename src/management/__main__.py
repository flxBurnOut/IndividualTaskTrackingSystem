from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .runtime import default_data_dir


def main():
    parser = argparse.ArgumentParser(description='个人事务管理 · 原生界面与 Codex 共用同一业务服务')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--verify-ui', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--choose-data', action='store_true')
    parser.add_argument('--launch-codex', action='store_true', help='兼容入口：打开管理软件并自动准备已启用的 Codex 连接')
    parser.add_argument('--install-shortcuts', action='store_true', help='为所选数据空间创建桌面与开始菜单快捷方式')
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--bootstrap-service', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--mcp', action='store_true')
    parser.add_argument('--mcp-discussion')
    parser.add_argument('--discussion-epoch')
    parser.add_argument('--diagnose', action='store_true')
    parser.add_argument('--source-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--document-worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source_worker:
        from .source_worker import worker
        return worker(args.source_worker)
    if args.document_worker:
        from .document_worker import worker
        return worker(args.document_worker)
    if args.bootstrap_service:
        from .runtime import bootstrap_service
        bootstrap_service(args.data_dir)
        return 0
    if args.service:
        from .service import run_service
        return run_service(args.data_dir)
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
    if args.choose_data:
        from .launcher import choose_data_dir
        selected = choose_data_dir()
        if selected is None:
            return 0
        args.data_dir = selected
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
        QMessageBox.information(None, '快捷方式已创建', '桌面和开始菜单中都可以找到“个人事务管理”。\n\n打开的数据空间：'+result['data_dir'])
        return 0
    from .gui import run
    return run(args.data_dir)


if __name__ == '__main__':
    sys.exit(main())
