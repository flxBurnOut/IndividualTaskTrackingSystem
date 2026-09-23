# 个人事务管理 · macOS

本项目是本地优先的个人事务管理桌面应用，日常入口为“今天、项目与课程、复盘”。界面、Codex MCP 接口和后台任务共用本机业务服务与 SQLite 数据库，无需 npm 或网页前端编译。

当前为 **`macos` 适配分支**，提供 Mac 源码启动入口和独立 `.app` 构建。Windows 版本请使用 [`main` 分支](https://github.com/aaaarthur666-design/IndividualTaskTrackingSystem/tree/main)。

## 分支与平台边界

| 分支 | 用途 |
| --- | --- |
| [`main`](https://github.com/aaaarthur666-design/IndividualTaskTrackingSystem/tree/main) | 原有 Windows 版本的维护与构建基线 |
| [`macos`](https://github.com/aaaarthur666-design/IndividualTaskTrackingSystem/tree/macos) | macOS 适配、运行验证与 `.app` 构建 |

Mac 适配独立提交到 `macos`，本次未合并到 `main`。Windows 用户应继续从 `main` 运行和构建。本分支包含部分共用运行代码的调整，尚未完成 Windows 实机回归；分支隔离不代表本分支在 Windows 上的行为与原版完全相同。

同时开发两端时，建议使用独立 Git worktree，并分别创建虚拟环境和构建产物。在已有仓库目录执行：

```sh
git fetch origin
git worktree add -b macos ../IndividualTaskTrackingSystem-macos origin/macos
```

如果本地已经存在 `macos` 分支，使用 `git worktree add ../IndividualTaskTrackingSystem-macos macos`；如果该分支已有 worktree，直接使用现有目录。可通过 `git worktree list` 查看。

## 当前验证状态

2026-09-23 在 **Apple Silicon / macOS 27.0** 上完成验证：

- Python 3.13.14、SQLite 3.53.1、PySide6 / Qt 6.11.2。
- **733 项测试、11 项子测试全部通过**；包含业务、备份恢复、GUI、资料、文档和 MCP 回归。
- 源码启动和独立应用均通过 Cocoa 原生窗口验证，确认中文字体、浅深色主题及 Retina 2× 显示。
- 应用移动至中文与空格路径、安装目录只读，以及通过 Finder 启动均通过验证。

构建目标为 macOS 13+，但最低版本、其他 macOS 版本及 Intel 尚未实机验收。当前应用使用本地 ad-hoc 签名，未完成 Developer ID 签名和 Apple 公证。真实 Codex 模型调用未纳入本次测试。

完整范围见 [macOS 测试报告](docs/macOS测试报告.md)；设计与发布计划见 [macOS 适配方案](docs/macOS适配方案.md)。

## 从源码运行

首次获取 Mac 版本：

```sh
git clone --branch macos --single-branch https://github.com/aaaarthur666-design/IndividualTaskTrackingSystem.git IndividualTaskTrackingSystem-macos
cd IndividualTaskTrackingSystem-macos
```

已有 Mac worktree 的用户直接进入对应目录。准备 Python 3.12+，然后执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m management.runtime_check
.venv/bin/python -m management
```

**Python 版本达标不代表 SQLite 达标。** 运行时需要 SQLite 3.51.3+，或已修复的 3.50.7 / 3.44.6 分支。若检查失败，应使用携带合适 SQLite 的新版 Python 重建虚拟环境，不要跳过检查；本次实测 Python 3.13.7 / SQLite 3.50.4 无法启动。可参考 [运行时准备说明](docs/macOS适配方案.md)。

依赖准备完成后，也可双击：

- `启动个人事务管理.command`：打开应用。
- `选择数据空间.command`：选择已有数据空间或创建新空间。

## 构建独立 Mac 应用

在已配置好的 Mac 开发环境中执行：

```sh
.venv/bin/python packaging/build_macos.py
```

Apple Silicon 构建输出：

```text
release/PersonalManagement-0.7-macos-arm64/PersonalManagement.app
release/PersonalManagement-0.7-macos-arm64.zip
```

双击 `.app` 即可运行，也可将完整应用拖入“应用程序”；使用独立应用无需另装 Python。Intel 需要在对应 x86_64 Mac/Python 环境单独构建，不能直接使用 arm64 包。

`release/`、`.venv/` 和测试数据不纳入 Git，因此克隆仓库不会获得已构建的应用。上述路径是本地构建输出，不是 GitHub 下载链接。正式分发的签名与公证流程见 [macOS 适配方案](docs/macOS适配方案.md)。

## 数据保存与 Codex 接入

新数据默认保存在：

```text
~/Library/Application Support/PersonalManagement/data
```

- 指定数据目录的优先级为 `--data-dir`、`PERSONAL_MANAGEMENT_DATA`、默认目录。
- 如果旧目录 `~/PersonalManagement/data` 已有 `database.sqlite3`，继续使用旧目录，不自动搬迁数据。
- 应用安装目录与业务数据分离。移动应用不会移动记录；通过选择数据空间或备份恢复切换目录。
- 关闭界面后，本机业务服务继续运行；不自动注册登录项，电脑睡眠或关机期间不保证准点执行定时任务。
- 在应用设置中复制当前数据空间的 MCP 配置，再加入 Codex。应用不会自行修改 Codex 配置或复制账号凭据。
- 移动应用前应退出该版本界面及后台服务；移动后重新复制 MCP 配置，因为配置使用应用的绝对路径。

发布包不包含业务数据，测试仅使用隔离的合成记录，不自动迁移原 Windows 数据。

## 运行测试

在已配置好的 Mac 开发环境中执行：

```sh
.venv/bin/python -m pip check
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

构建完成后验证真实窗口、打包后的业务流程和 Finder 启动：

```sh
QT_QPA_PLATFORM=cocoa .venv/bin/python packaging/smoke.py
.venv/bin/python packaging/smoke_macos_launch.py
```

原生窗口测试需要桌面会话和本机回环端口，会打开测试窗口，完成后清理对应测试服务。`.build/` 和 `.test-output/` 保存本地验证证据，不随 Git 提交；历史执行结果见 [测试报告](docs/macOS测试报告.md)。

## 功能与文档

每日复盘逐项选择完成或未完成，未选保持未知；资料随所属项目保存受管理副本；支持课件、通知、邮件、截图与网页，并可让 Codex 整理课程信息。复盘时间可在设置中调整。

完整历史功能方案仍有后续工作，不能把本版视为全部 49 类场景已验收，详见 [功能实现对照](docs/功能实现对照.md)。

- [macOS 适配方案](docs/macOS适配方案.md) · [macOS 测试报告](docs/macOS测试报告.md)
- [Codex 接入与推理边界](docs/Codex接口与后台推理.md) · [开发与扩展](docs/开发与扩展.md)
- [原 Windows 使用说明](docs/使用说明.md) · [原实施验收记录](实施验收记录.md)
- [界面约定](UX_V3_CONTRACT.md) · [实施前审查](实施前审查报告_2026-09-22.md)
- [核心业务设计](核心业务层执行设计_2026-09-22.md) · [层级与界面规范](数据层级与界面扩展规范_2026-09-22.md)
- [资源预算](资源预算与恢复规范_2026-09-22.md) · [历史场景](历史场景覆盖矩阵_2026-09-22.md)

`.analysis` 为历史审查资料，不属于应用发布包或业务数据。
