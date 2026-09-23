"""Check the SQLite runtime before creating a space or building a release."""
import sqlite3

from .schemas import BusinessError


def check_sqlite():
    version = sqlite3.sqlite_version_info
    supported = (version >= (3, 51, 3)
                 or version[:2] == (3, 50) and version >= (3, 50, 7)
                 or version[:2] == (3, 44) and version >= (3, 44, 6))
    if not supported:
        raise BusinessError('sqlite_version',
                            f'SQLite {sqlite3.sqlite_version} 缺少所需的 WAL 修复。'
                            '请使用 SQLite 3.51.3+（或 3.50.7 / 3.44.6 分支）的 Python 重建 .venv，'
                            '或使用随应用提供的运行时。')


if __name__ == '__main__':
    try:
        check_sqlite()
    except BusinessError as error:
        raise SystemExit(error.message)
    print(f'SQLite {sqlite3.sqlite_version}: 运行时检查通过')
