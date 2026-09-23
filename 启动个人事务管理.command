#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print '请先在项目目录运行：python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"'
  read -r '?按回车退出…'
  exit 1
fi
.venv/bin/python -m management.runtime_check
exec .venv/bin/python -m management "$@"
