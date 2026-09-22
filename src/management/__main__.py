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
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--bootstrap-service', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--mcp', action='store_true')
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
    from .gui import run
    return run(args.data_dir)


if __name__ == '__main__':
    sys.exit(main())
