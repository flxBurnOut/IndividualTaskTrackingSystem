# macOS 适配方案

## 范围与架构

保留 Python 3.12+、PySide6、SQLite 与本机 HTTP 服务，不重写业务层。桌面界面、MCP、资料解析、文档执行器共享同一数据空间和版本校验。目标是 macOS 13+，Apple Silicon 与 Intel 分别构建；实际验证系统、架构和未验证事项见 `macOS测试报告.md`。最低系统版本是依赖与打包配置目标，不代表已在所有系统实测。

## 启动与安装

源码开发：在仓库目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m management.runtime_check
.venv/bin/python -m management
```

也可以双击 `启动个人事务管理.command`；`选择数据空间.command` 打开数据空间选择器。已有 Python 需要 3.12 或以上。脚本以自身位置定位项目，支持中文和空格路径，不依赖终端当前目录。

仅满足 Python 版本还不够：项目原有检查要求 SQLite 3.51.3+，或已修复的 3.50.7 / 3.44.6 分支。本机旧 Python 3.13.7 的 SQLite 3.50.4 实测无法启动。若检查失败，可安装新版 Python 后重建虚拟环境；安装了 uv 的机器也可以运行 `uv python install 3.13`、`uv venv --python 3.13 .venv`、`uv pip install --python .venv/bin/python -e '.[dev]'`。已有虚拟环境请先备份或明确重建。此次验证使用 Python 3.13.14 / SQLite 3.53.1；独立 `.app` 将把经过检查的运行时一同打包。

独立应用：执行 `.venv/bin/python packaging/build_macos.py`，得到 `release/PersonalManagement-0.7-macos-<架构>/PersonalManagement.app` 和 ZIP。可将整个 `.app` 拖到“应用程序”后打开，无需安装 Python。移动应用前退出该版本 GUI 和其后台服务；移动后重新复制 MCP 配置，因为其中使用应用绝对路径。

`.app/Contents/MacOS` 内含图形入口 `PersonalManagement` 和支持标准输入输出的 `PersonalManagementService`。业务服务、MCP、文档与资料子进程都使用同一个服务入口。Qt、Python、时区数据和内置课程技能随应用打包；账号、数据库和测试记录不打包。

## 数据与生命周期

新数据默认位于 `~/Library/Application Support/PersonalManagement/data`。优先级为 `--data-dir`、`PERSONAL_MANAGEMENT_DATA`、默认目录。旧版 `~/PersonalManagement/data/database.sqlite3` 存在时继续使用该目录，避免升级后看起来丢失记录；不自动迁移或复制。Windows 默认目录保持原有行为。

通过选择数据空间或现有备份/恢复功能切换目录。数据库应在本机磁盘，不建议把运行中的 SQLite 目录放入同步盘。应用安装目录可只读，业务文件写入所选数据目录。

GUI 关闭（包括 Command-W / Command-Q）前保留现有未保存复盘提示并等待正在进行的请求结束。业务服务继续运行，MCP 退出也不影响它；不自动注册登录项或系统服务。关机/重启后在下次打开应用或 MCP 时启动，电脑睡眠或关机期间不保证准点触发，恢复后的补偿规则沿用原业务逻辑。如需结束测试服务，测试脚本仅终止它自己创建的合成数据空间对应进程。

## 系统体验、字体与文件

- 增加 macOS 原生菜单与 Command-F、Command-逗号、Command-W、Command-Q；编辑控件沿用 Qt 标准复制、粘贴和中文输入行为。
- 使用 PingFang SC 作为 Mac 默认字体。跨平台导入的不可用字体回退到本机默认；保留用户保存的字体选择。继续支持浅/深主题和字号调整，Qt 负责 Retina 缩放。
- DOCX 使用 Mac 中文字体名称；PDF 依次尝试本机宋体、黑体、Arial Unicode，并验证字符覆盖后嵌入。允许 `PERSONAL_MANAGEMENT_PDF_FONT` 指向自备 TTF/TTC。字体不随应用分发；缺字时明确报错，避免产生空白或方块 PDF。
- 文件选择和打开沿用 Qt 的本机对话框与 QDesktopServices。资料保存受管理副本，原件删除后仍可访问。测试覆盖中文/空格路径、PDF 文字与扫描件解析。
- macOS 的 `/var`、`/tmp`、`/etc` 系统别名仅在指向预期 `/private/...` 目标时展开，再逐层检查剩余路径。用户创建的文件/目录符号链接仍拒绝；不通过全路径 `resolve()` 绕过资源防护。

## Codex 与 MCP

用户指定可执行文件优先，其次 PATH；Mac 补充 Homebrew、用户 `.local/bin` 与常见应用安装路径。Finder 启动时也能找到 Homebrew 中的 Codex。发现可执行文件不代表已登录或模型调用可用；实际推理沿用用户启用和候选确认流程。

设置页生成当前应用/虚拟环境的 MCP 配置；不解析掉 venv 的 Python 符号链接，不写用户配置、不复制凭据。业务服务仅监听 127.0.0.1，标准输入输出专供 MCP 协议。

## 测试与验收

1. 安装依赖并执行 `pip check`。
2. 全量 pytest：业务、备份恢复、事务、GUI、定时逻辑、资料、文档、MCP；数据均为隔离的合成记录。
3. Mac 专项：数据目录优先级/旧目录兼容、venv 路径、打包辅助进程、字体回退与 PDF 中文提取。
4. Cocoa 原生窗口：启动、浅/深色、菜单、界面截图；区别于 offscreen 自动化测试。
5. 冻结应用：独立启动服务与 GUI、双 MCP 客户端/退出存活、任务/复盘/课表、资料解析与文档子进程；以清理 Python 环境变量后的打包程序为被测对象。
6. 发布矩阵：macOS 13 与当前稳定版本、arm64 与 x86_64、干净用户环境、中文输入法/Retina/休眠恢复和 Gatekeeper。当前机器无法覆盖的项目保留为发布前验收项，不据单机结果宣称全平台通过。

## 签名与发布

本地默认使用 PyInstaller ad-hoc 签名，可执行 `codesign --verify --deep --strict <应用路径>` 检查完整性。对外正式分发需用户的 Apple Developer ID：以 `--codesign-identity` 构建，再使用 `xcrun notarytool submit ... --keychain-profile ... --wait` 公证、`xcrun stapler staple ...` 装订票据，最后重新制作 ZIP 并在下载后的干净机器测试 Gatekeeper。没有开发者证书与公证凭据时仅交付本地测试包，不宣称已通过公证，不要求用户全局关闭 Gatekeeper。

技术依据：[Qt 支持平台](https://doc.qt.io/qtforpython-6/overviews/qtdoc-supported-platforms.html)、[PyInstaller macOS 架构与签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)、[PyInstaller spec 与 BUNDLE](https://pyinstaller.org/en/stable/spec-files.html)。
