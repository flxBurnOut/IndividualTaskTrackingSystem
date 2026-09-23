# macOS 适配测试报告

测试日期：2026-09-23。仓库基线：`5378747`，加本次工作区的 macOS 适配修改。所有业务操作均使用隔离的合成数据；没有迁移个人资料、修改 Codex 账号或写入用户 MCP 配置。

## 结论

在本机 Apple Silicon / macOS 27.0 上，源码启动与独立 `.app` 均通过运行验证；最终 **733 项测试、11 项子测试通过，0 失败**。已生成可本地使用的 arm64 应用。Intel、较早 macOS、正式签名与公证仍需对应环境验证，不能把本报告视为所有 Mac 的发布认证。

## 环境

| 项目 | 实际版本 |
| --- | --- |
| 系统 | macOS 27.0，Build 26A428 |
| 架构 | arm64，Apple Silicon |
| Python | 3.13.14，项目隔离运行时 |
| SQLite | 3.53.1 |
| PySide6 / Qt | 6.11.2 |
| PyInstaller | 6.22.3 |
| 原生窗口插件 | cocoa |
| 字体 / 显示倍率 | PingFang SC / 2.0 |

最初本机 Python 3.13.7 搭配 SQLite 3.50.4，被项目原有 WAL 修复版本检查拒绝。后续改用项目 `.build/python` 内下载的 Python 3.13.14 重建 `.venv`，未更改系统 Python，也未降低数据库版本要求。项目移动后如继续使用源码，应重建虚拟环境；独立 `.app` 不依赖这个目录。

## 验证结果

| 验证内容 | 结果与证据 |
| --- | --- |
| 依赖完整性 | `pip check`：No broken requirements found |
| 全量业务与界面回归 | 733 passed, 11 subtests passed，50.18 秒；GUI 自动化使用 offscreen |
| 数据目录与运行时 | Mac 默认目录、旧目录兼容、环境变量覆盖、SQLite 检查、venv Python 路径均通过 |
| 资源路径安全 | 系统 `/tmp`、`/var/tmp` 可用；嵌套目录链接和文件链接仍被拒绝；备份恢复与导出测试通过 |
| 源码双击脚本 | 从 `/private/tmp` 启动脚本，中文/空格数据目录，真实 Cocoa 窗口成功加载 |
| 独立 `.app` | GUI 与辅助服务均为 arm64 Mach-O；服务使用包内 Python/SQLite |
| 原生界面 | 浅色、深色、18px 字号可加载；截图已目视检查，中文正常、内容无明显裁切 |
| Mac 快捷键 | 菜单注册为 ⌘,、⌘F、⌘W、⌘Q；未将此登记检查等同于全部物理键盘交互验收 |
| MCP 与服务生命周期 | 标准 stdio 客户端通信通过；双客户端回归通过；GUI/MCP 退出后业务服务继续可用 |
| 核心业务闭环 | 新建任务、每日复盘幂等、复盘偏好、周期任务、课表教学周/假期、删除恢复通过 |
| 资料和文档子进程 | 原件删除后的受管理资料可读；扫描 PDF 与课表 PDF 图像解析、DOCX、中文 PDF 生成通过 |
| 中文 PDF 内容 | 字体嵌入检查和中文正文提取回读通过；不只验证文件存在 |
| Finder / 应用移动 | 应用副本移到中文与空格路径，安装目录只读；通过 `open -n -W` / LaunchServices 成功启动 Cocoa 窗口 |
| 干净启动环境 | 移动后的辅助程序在 `/private/tmp`、仅 `/usr/bin:/bin` PATH、无 PYTHONPATH/VIRTUAL_ENV 环境下通过诊断 |
| 签名完整性 | `codesign --verify --deep --strict` 通过；当前为 ad-hoc，未公证 |
| 发布包内容 | 检查未发现数据库、runtime.json、auth.json、测试目录等禁止发布内容 |

初轮全量测试暴露了 macOS `/var` 系统别名被误判为不安全链接的问题；修复保留逐层链接检查。对应的模拟链接测试夹具同时识别原始和展开后的路径，最终实际链接与模拟链接回归均通过。

Codex 可执行文件发现确认到 `/opt/homebrew/bin/codex`。此次未发起真实模型推理、未消费模型额度；后台 AI 协议的合成测试和本地 MCP 通信通过不代表实际账号/模型调用已验收。

## 产物与复现

- 应用：`release/PersonalManagement-0.7-macos-arm64/PersonalManagement.app`（约 161 MiB）。双击即可打开，也可将完整 `.app` 拖入“应用程序”。
- 压缩包：`release/PersonalManagement-0.7-macos-arm64.zip`（约 73.8 MiB）。
- SHA-256：`58e216bd900c0a79235da1bc2641bb4da11a3e142eece61a63386ee856342f10`。
- 方案：`docs/macOS适配方案.md`。

在已安装依赖、SQLite 检查通过的环境运行：

```sh
.venv/bin/python -m management.runtime_check
.venv/bin/python -m pip check
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q --junitxml=.build/macos-pytest.xml
.venv/bin/python packaging/build_macos.py
QT_QPA_PLATFORM=cocoa .venv/bin/python packaging/smoke.py
.venv/bin/python packaging/smoke_macos_launch.py
```

测试需要本机回环端口和桌面会话；受限执行沙箱可能禁止监听端口，这与应用兼容性不同。运行脚本会打开测试窗口并在完成后关闭，仅终止自己创建的数据空间对应服务。

本地原始证据（这些目录被 git 忽略）：

- `.build/macos-pytest.log`、`.build/macos-pytest.xml`：全量测试结果。
- `.build/macos-build.json`、`.build/macos-build.log`：构建与签名信息。
- `.build/packaged-smoke.json`、`.build/macos-packaged-smoke.log`：打包后的端到端验证。
- `.build/macos-launch-smoke.json`、`.build/macos-launch-smoke.log`：源码脚本、只读移动应用和 Finder 验证。
- 两份 smoke JSON 的 `screenshot` 字段指向 `.test-output` 中保留的浅/深色及 Finder 窗口截图。

## 发布前尚需验证

Intel 架构实机、macOS 13 最低目标与其他系统版本、不同屏幕和完整中文输入法交互、长时间休眠恢复、系统文件访问授权对话框、真实 Codex 模型调用、Developer ID 签名/Apple 公证/Gatekeeper 下载路径。Windows 本次未重新实机验证。上述项目已纳入适配方案的发布矩阵，不计入本机通过数。
