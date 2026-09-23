#!/bin/zsh
set -eu
exec "${0:A:h}/启动个人事务管理.command" --choose-data "$@"
