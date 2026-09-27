"""Prepare a normal Codex dialogue workspace for one business data space.

This module prepares local files only. It does not register a Codex desktop
project, trust a directory, start a model turn, or change business records.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib


WORKSPACE_NAME = "Codex事务助手"
MAX_FILE_BYTES = 512 * 1024
_DOCUMENT_PREFIX = "<!-- personal-management managed document sha256="
_CONFIG_PREFIX = "# >>> personal-management managed MCP sha256="
_CONFIG_END = "# <<< personal-management managed MCP"
_INSTRUCTIONS_PREFIX = "# >>> personal-management managed instructions sha256="
_INSTRUCTIONS_END = "# <<< personal-management managed instructions"
_LEGACY_CONFIG_MARKER = "# Managed local business connection; regenerate after selecting a new installed release."
_OLD_CONNECTION = "项目级 `.codex/config.toml` 已提供MCP配置，直接连接明确安装的发布版本，使用本工作区上一级的数据空间。软件升级或迁移后，通过项目根tools/prepare_codex_workspace.py更新连接配置；不要自行挑选发布目录中看起来版本最高的程序。未连接时先报告接口状态并检查配置，不绕过接口改库。不要读取或输出runtime.json中的令牌。"
_NEW_CONNECTION = "项目级 `.codex/config.toml` 由个人事务管理的 Codex 设置自动准备，连接同一数据空间的业务服务。软件升级或迁移后，在个人事务管理中重新保存 Codex 设置即可修复连接，再新建或重新打开普通任务以加载配置。未连接时先报告接口状态并检查配置，不绕过接口改库。不要读取或输出runtime.json中的令牌。"
_README = """# Codex 事务助手

这里是个人事务管理的普通对话工作区；项目是否已在 Codex 中登记，以软件的连接状态提示为准。

在这个项目中可以为不同事件分别新建普通任务，粘贴群聊、邮件正文、截图或文件，直接说明需要记录、更新或安排什么。Agent 会先读取最新业务数据，再通过本地 MCP 接口更新同一份软件数据；不同任务不必共享完整聊天记录。

软件生成的“个人事务 · 每日计划”等会话用于后台生成候选。日常突发事件请使用本项目的普通任务。

原文件仍保存在数据空间中，当前事项和记忆以业务接口为准。只打开 Codex 也可以使用业务接口；连接时会按需启动本地业务服务。

首次打开项目时，如 Codex 提示信任本地项目，请确认这是自己选择的数据空间。项目级配置只有在 Codex 允许加载时才生效。

软件升级或迁移后，在个人事务管理中重新保存 Codex 设置即可修复连接，再新建或重新打开普通任务以加载配置。不要手动修改本工作区的受管理连接配置；个人项目设置及其他 MCP 连接可写在受管理区之外。
"""
_LEGACY_README = """在Codex中将这个文件夹添加为本地项目，从项目中新建普通任务。

可以粘贴群聊、邮件正文、截图或文件，并直接说明需要记录、更新或安排什么。
Agent会先读取最新业务数据，再通过本地接口更新同一份软件数据。
不同事件可分别开新任务，无需把上下文一直挤进同一段聊天。

软件生成的“个人事务 · 每日计划”等是后台候选会话；日常突发事件请使用项目内的普通新任务。
旧原文件仍保存，当前事实以软件业务接口为准。

升级软件或迁移整个目录后，运行项目根tools/prepare_codex_workspace.py更新连接，再重新打开普通任务以加载新配置。
"""


class WorkspaceError(ValueError):
    """Workspace files cannot safely be prepared without changing user content."""


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n")


def _digest(text: str) -> str:
    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def mcp_config(data_dir) -> str:
    """Return portable stdio MCP configuration for this installed runtime."""
    executable = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        executable = executable.with_name("PersonalManagementService.exe")
        if not executable.is_file():
            raise WorkspaceError("未找到随应用提供的业务服务程序，请保持软件目录完整。")
        args = ["--mcp", "--data-dir", str(Path(data_dir).resolve())]
    else:
        if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").is_file():
            executable = executable.with_name("python.exe")
        if not executable.is_file():
            raise WorkspaceError("未找到当前 Python 运行程序，无法配置 Codex 业务连接。")
        args = ["-m", "management", "--mcp", "--data-dir", str(Path(data_dir).resolve())]
    lines = [
        "[mcp_servers.personal_management]",
        "command = " + _json(str(executable)),
        "args = " + _json(args),
        "enabled = true",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 300",
    ]
    if not frozen:
        lines.extend([
            "", "[mcp_servers.personal_management.env]",
            "PYTHONPATH = " + _json(str(Path(__file__).resolve().parents[1])),
        ])
    return "\n".join(lines) + "\n"


def _assert_plain(path: Path) -> None:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise WorkspaceError(f"对话工作区包含链接路径，未修改：{path}")


def _read(path: Path) -> bytes | None:
    _assert_plain(path)
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorkspaceError(f"无法读取对话工作区文件，未覆盖：{path}（{error}）") from error
    if len(content) > MAX_FILE_BYTES:
        raise WorkspaceError(f"对话工作区配置过大，未覆盖：{path}")
    return content


def _decode(content: bytes | None, path: Path) -> str:
    if content is None:
        return ""
    try:
        return content.decode("utf-8-sig")
    except UnicodeError as error:
        raise WorkspaceError(f"对话工作区文件不是 UTF-8 文本，未覆盖：{path}") from error


def _document(body: str) -> str:
    return _DOCUMENT_PREFIX + _digest(body) + " -->\n" + body


def _managed_document(text: str) -> bool:
    match = re.match(re.escape(_DOCUMENT_PREFIX) + r"([0-9a-f]{64}) -->\r?\n", text)
    return bool(match and _digest(text[match.end():]) == match.group(1))


def _agents_text(previous: str, template: str) -> str:
    legacy = template.replace(_NEW_CONNECTION, _OLD_CONNECTION)
    known_previous = {'5d29dfb95d44e3c48f962e99e0c0503328533a36223bea32cbd58f3a8338548d',
                      '4de008f87872d963e6c4d22cfd4fbcfb81537bafe33a90e37643013081a4711e'}
    if previous and not _managed_document(previous) and _normalized(previous) not in {template, legacy} and _digest(previous) not in known_previous:
        raise WorkspaceError("Codex事务助手的 AGENTS.md 含有用户修改，已保留。请先合并个人说明，再重新保存 Codex 设置。")
    return _document(template)


def _parse_config(text: str) -> dict:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise WorkspaceError("Codex事务助手的项目配置无法解析，已保留。请修复 .codex/config.toml 后重试。") from error
    if not isinstance(parsed.get("mcp_servers", {}), dict):
        raise WorkspaceError("Codex事务助手的 mcp_servers 配置格式异常，已保留。")
    return parsed


def _legacy_connection(server) -> bool:
    if not isinstance(server, dict) or set(server) != {"command", "args", "enabled", "startup_timeout_sec", "tool_timeout_sec"}:
        return False
    args = server.get("args")
    command = server.get("command")
    return (
        isinstance(command, str)
        and command.replace("\\", "/").rsplit("/", 1)[-1] == "PersonalManagementService.exe"
        and isinstance(args, list) and len(args) == 3
        and args[:2] == ["--mcp", "--data-dir"] and isinstance(args[2], str)
        and server["enabled"] is True and server["startup_timeout_sec"] == 30
        and server["tool_timeout_sec"] == 300
    )


def _configuration(previous: str, connection: str) -> str:
    parsed = _parse_config(previous)
    desired = _parse_config(connection)["mcp_servers"]["personal_management"]
    block = _CONFIG_PREFIX + _digest(connection) + "\n" + connection + _CONFIG_END + "\n"
    if _CONFIG_PREFIX in previous or _CONFIG_END in previous:
        starts = list(re.finditer(r"(?m)^" + re.escape(_CONFIG_PREFIX) + r"([0-9a-f]{64})\r?\n", previous))
        ends = list(re.finditer(r"(?m)^" + re.escape(_CONFIG_END) + r"(?:\r?\n|$)", previous))
        if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
            raise WorkspaceError("Codex事务助手的受管理连接标记不完整，已保留原配置。")
        start, end = starts[0], ends[0]
        if _digest(previous[start.end():end.start()]) != start.group(1):
            raise WorkspaceError("Codex事务助手的受管理 MCP 连接已被修改，已保留。请先处理冲突，再重新保存 Codex 设置。")
        text = previous[:start.start()] + block + previous[end.end():]
    elif previous.startswith(_LEGACY_CONFIG_MARKER):
        server = parsed.get("mcp_servers", {}).get("personal_management")
        if not _legacy_connection(server):
            raise WorkspaceError("旧版 Codex 事务连接含有用户修改，已保留原配置。")
        # The old helper emitted exactly this header. Other TOML sections are
        # deliberately retained rather than serialized or reformatted.
        match = re.search(r"(?m)^\[mcp_servers\.personal_management\]\r?\n", previous)
        if match is None:
            raise WorkspaceError("旧版 Codex 事务连接无法安全定位，已保留原配置。")
        following = re.search(r"(?m)^\s*\[", previous[match.end():])
        end = match.end() + following.start() if following else len(previous)
        prefix = previous[:match.start()]
        prefix = prefix[len(_LEGACY_CONFIG_MARKER):].lstrip("\r\n")
        text = prefix + block + previous[end:]
    else:
        if "personal_management" in parsed.get("mcp_servers", {}):
            raise WorkspaceError("项目已存在用户配置的 personal_management 连接，已保留。请先处理同名连接，再重新保存 Codex 设置。")
        text = previous + ("\n\n" if previous and not previous.endswith("\n") else "\n" if previous else "") + block
    result = _parse_config(text)
    if result.get("mcp_servers", {}).get("personal_management") != desired:
        raise WorkspaceError("项目中的其他配置改变了事务连接，已保留原配置。")
    before_other = {key: value for key, value in parsed.items() if key != "mcp_servers"}
    after_other = {key: value for key, value in result.items() if key != "mcp_servers"}
    before_servers = {key: value for key, value in parsed.get("mcp_servers", {}).items() if key != "personal_management"}
    after_servers = {key: value for key, value in result.get("mcp_servers", {}).items() if key != "personal_management"}
    if before_other != after_other or before_servers != after_servers:
        raise WorkspaceError("无法在保持其他项目配置的前提下修复连接，已保留原配置。")
    return text


def _instructions_configuration(previous: str, instructions_path: Path) -> str:
    """Own one root key independently of the table-scoped MCP configuration.

    TOML comments do not end a table. The block must therefore precede all
    existing sections, even when the managed MCP block appears after a user's
    custom table. Other project text remains byte-for-byte within this string.
    """
    parsed = _parse_config(previous)
    remainder = previous
    if _INSTRUCTIONS_PREFIX in previous or _INSTRUCTIONS_END in previous:
        starts = list(re.finditer(r"(?m)^" + re.escape(_INSTRUCTIONS_PREFIX) + r"([0-9a-f]{64})\r?\n", previous))
        ends = list(re.finditer(r"(?m)^" + re.escape(_INSTRUCTIONS_END) + r"(?:\r?\n|$)", previous))
        if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
            raise WorkspaceError("Codex事务助手的受管理指令配置标记不完整，已保留原配置。")
        start, end = starts[0], ends[0]
        owned_body = previous[start.end():end.start()]
        if _digest(owned_body) != start.group(1):
            raise WorkspaceError("Codex事务助手的受管理指令配置已被修改，已保留原配置。")
        owned = _parse_config(owned_body)
        if (set(owned) != {"model_instructions_file"}
                or not isinstance(owned["model_instructions_file"], str)
                or parsed.get("model_instructions_file") != owned["model_instructions_file"]):
            raise WorkspaceError("Codex事务助手的受管理指令配置不在项目顶层，已保留原配置。")
        remainder = previous[:start.start()] + previous[end.end():]
    remaining = _parse_config(remainder)
    if "model_instructions_file" in remaining:
        raise WorkspaceError("项目已有用户配置的 model_instructions_file，已保留其配置和文件。请先处理指令配置冲突，再重新保存 Codex 设置。")
    body = "model_instructions_file = " + _json(str(instructions_path.resolve())) + "\n"
    block = _INSTRUCTIONS_PREFIX + _digest(body) + "\n" + body + _INSTRUCTIONS_END + "\n"
    text = block + remainder
    result = _parse_config(text)
    if (result.get("model_instructions_file") != str(instructions_path.resolve())
            or {key: value for key, value in result.items() if key != "model_instructions_file"} != remaining):
        raise WorkspaceError("无法在保持其他项目配置的前提下准备指令文件，已保留原配置。")
    return text


def _atomic_write(path: Path, content: bytes, expected: bytes | None) -> None:
    """Replace one complete file, refusing edits observed since preflight."""
    descriptor, temporary = tempfile.mkstemp(prefix=".pm-workspace-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(path) != expected:
            raise WorkspaceError(f"对话工作区文件在准备过程中发生变化，已保留新内容，请重试：{path}")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare_workspace(data_dir) -> dict:
    """Create/update managed guidance and MCP files, preserving user settings.

    All conflicts are checked before any file is changed. Each changed file is
    replaced atomically. The result intentionally says nothing about desktop
    project registration or trust: those are separate operations.
    """
    data = Path(data_dir).resolve()
    workspace = data / WORKSPACE_NAME
    config_dir = workspace / ".codex"
    for directory in (workspace, config_dir):
        _assert_plain(directory)
        if directory.exists() and not directory.is_dir():
            raise WorkspaceError(f"对话工作区目录被同名文件占用：{directory}")
    paths = {
        "agents_path": workspace / "AGENTS.md",
        # Prepare the referenced document before publishing its config key.
        "instructions_path": config_dir / "management-instructions.md",
        "config_path": config_dir / "config.toml",
        "readme_path": workspace / "开始使用.md",
    }
    originals = {key: _read(path) for key, path in paths.items()}
    previous = {key: _decode(originals[key], path) for key, path in paths.items()}
    template_path = Path(__file__).parent / "builtin_skills" / "codex-workspace" / "AGENTS.md"
    template = _normalized(_decode(_read(template_path), template_path))
    if not template or _NEW_CONNECTION not in template:
        raise WorkspaceError("软件缺少 Codex 对话工作区模板，请恢复完整安装后重试。")
    from .workspace_instructions import instructions
    body = instructions()
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_FILE_BYTES - 200:
        raise WorkspaceError("软件的 Codex 工作区指令缺失或过大，请恢复完整安装后重试。")
    if originals["instructions_path"] is not None and not _managed_document(previous["instructions_path"]):
        raise WorkspaceError("Codex事务助手的 management-instructions.md 含有用户修改，已保留原文件。请先处理指令文件冲突，再重新保存 Codex 设置。")
    generated = {
        "agents_path": _agents_text(previous["agents_path"], template),
        "instructions_path": _document(body),
        "config_path": _instructions_configuration(
            _configuration(previous["config_path"], mcp_config(data)), paths["instructions_path"]),
    }
    readme = previous["readme_path"]
    readme_preserved = bool(readme and not _managed_document(readme) and _normalized(readme) not in {_README, _LEGACY_README})
    generated["readme_path"] = readme if readme_preserved else _document(_README)
    encoded = {key: value.encode("utf-8") for key, value in generated.items()}
    changed = [key for key in paths if encoded[key] != originals[key]]
    if readme_preserved:
        # Preserve BOM and newline choices in a user-owned readme as well.
        encoded["readme_path"] = originals["readme_path"]
        changed = [key for key in changed if key != "readme_path"]
    config_dir.mkdir(parents=True, exist_ok=True)
    for key in changed:
        _atomic_write(paths[key], encoded[key], originals[key])
    return {
        "workspace": str(workspace), "data_dir": str(data),
        **{key: str(path) for key, path in paths.items()},
        "changed": bool(changed), "changed_files": [str(paths[key]) for key in changed],
        "readme_preserved": readme_preserved,
    }
