# 个人事务管理

Windows 原生个人事务管理软件，当前版本 0.11.2。基于 Python 3.12、PySide6 和 SQLite，桌面界面、MCP 接口与后台作业共用本机业务服务。

## 功能

- 课程、项目、任务、每日计划、复盘及补欠记录。
- 总览、每周课表、教学周与假期设置、日常习惯和提前提醒。
- 资料托管、原文件目录、备份与恢复。
- 可选的本机 Codex 协助：资料整理、计划候选和共享业务记录的普通对话入口。

## 从源码运行

需要 Python 3.12 或更高版本。在 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m management --data-dir .local-data
```

首次运行在指定目录建立空数据空间。Codex 协助需要本机另行安装、登录并在软件设置中启用；手动管理功能不需要模型连接。

## 构建与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-output/pytest
.\.venv\Scripts\python.exe packaging/build.py
```

构建结果位于 `release/PersonalManagement-0.11.2/`。可打开 `PersonalManagement.exe`，或使用项目根目录的 `启动个人事务管理.vbs`（使用项目下的 `data` 数据空间）。便携版默认使用 `%LOCALAPPDATA%/PersonalManagement/data`。

## 仓库范围与数据边界

仓库保留软件源码、内置通用指令模板、图标、依赖声明、打包配置、自动化测试与通用使用文档。测试使用隔离的合成记录；兼容旧格式的导入代码不附带任何导入快照。

个人数据库、课件与附件、会话记录、账号、个人偏好、实际 MCP 配置、迁移备份、发布包、临时检查产物及历史设计／验收报告均不纳入后续提交。根目录和文档／打包目录采用明确的允许清单；新增分发文件时需同步调整 `.gitignore`。

这些规则适用于当前和后续版本，不会自动移除既有 Git 历史中的文件。

## 文档与边界

[使用说明](docs/使用说明.md) · [Codex 接口](docs/Codex接口与后台推理.md) · [开发与扩展](docs/开发与扩展.md) · [会话与原文件目录](docs/固定会话与原文件目录.md)

保存、导入、到课、完成、提交与掌握是不同状态。模型生成的是待核对候选；业务事实以本机服务成功保存的记录为准。电脑关闭期间不执行定时任务。自动化测试不能替代真实模型、安装包和长时间稳定性验收。
