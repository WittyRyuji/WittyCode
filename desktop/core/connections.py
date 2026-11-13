"""Local Connections: read-only Codex-format discovery, explicit import/trust, switches, config and stdio MCP.

Only this application's registry is written. Discovering a project never executes its config. An import previews all
commands and trusts their exact definition/source fingerprint together; a changed source needs another confirmation.
Remote OAuth, marketplace installation and non-command lifecycle handlers are deliberately outside this adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from typing import Any

from desktop.core.harness.mcp import McpClient
from desktop.core.memory.launch import McpCommand
from desktop.core.memory.safety import find_secret
from desktop.core.settings import default_settings_path
from desktop.core.winproc import CREATE_NO_WINDOW

KINDS = ("skill", "plugin", "hook", "mcp")
HOOK_EVENTS = frozenset({"SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"})
LIMIT = 2_000_000
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


class ConnectionError(ValueError):
    pass


def _local(path: str | Path) -> Path:
    raw = str(path)
    if raw.startswith(("\\\\", "//")) or "\x00" in raw:
        raise ConnectionError("连接配置只能从本地文件导入。")
    path = Path(os.path.abspath(os.path.expanduser(raw)))
    for part in reversed((path, *path.parents)):
        try:
            if _linked(part):
                raise ConnectionError("连接来源含目录链接，请先提供无链接的本地副本。")
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ConnectionError("连接来源路径不可访问，请检查文件夹和权限。") from error
    return path


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > LIMIT:
            raise ConnectionError("连接配置文件过大。")
        content = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(content) if path.suffix == ".toml" else json.loads(content)
        return dict(data) if isinstance(data, Mapping) else {}
    except (OSError, ValueError) as error:
        if isinstance(error, ConnectionError):
            raise
        raise ConnectionError("无法读取连接配置，请检查 JSON／TOML 格式和文件权限。") from error


def _digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _source_hash(path: Path) -> str:
    path = _local(path)
    if not path.exists():
        raise ConnectionError("连接来源已经不存在，请重新选择。")
    if path.is_file():
        if path.stat().st_size > LIMIT:
            raise ConnectionError("连接配置文件过大。")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    count, size = 0, 0
    for current, folders, names in os.walk(path, followlinks=False):
        if any(_linked(Path(current) / name) for name in folders):
            raise ConnectionError("插件中存在目录符号链接，请先提供不包含链接的本地副本。")
        folders[:] = sorted(name for name in folders if name not in {"node_modules", ".git", "__pycache__"})
        for name in sorted(names):
            file = Path(current) / name
            if _linked(file):
                raise ConnectionError("插件中存在符号链接，请先提供不包含链接的本地副本。")
            count += 1
            size += file.stat().st_size
            if count > 1000 or size > 12_000_000:
                raise ConnectionError("插件超过本地导入上限（1000 个文件／12 MB）。")
            digest.update(file.relative_to(path).as_posix().encode("utf-8"))
            digest.update(file.read_bytes())
    return digest.hexdigest()


def codex_home(home: Path) -> Path:
    """CODEX_HOME replaces the default; an explicitly isolated test home never reads the real profile."""
    if home == Path.home() and os.environ.get("CODEX_HOME"):
        return Path(os.path.abspath(os.path.expanduser(os.environ["CODEX_HOME"])))
    return home / ".codex"


def _entry(kind, name, source, config, project="", parent="", *, fingerprint=True):
    source = _local(source)
    return {"id": _digest([kind, str(source), name, project])[:24], "kind": kind, "name": str(name), "source": str(source), "project": str(project), "config": config, "parent": parent, "enabled": False, "imported": False, "fingerprint": _source_hash(source) if fingerprint else ""}


def _safe_member(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ConnectionError("插件清单只能使用插件目录内的相对路径。")
    path = _local(root / relative)
    if not path.is_relative_to(root):
        raise ConnectionError("插件清单路径越出了插件目录。")
    return path


class ConnectionStore:
    def __init__(self, file: Path | None = None, *, home: Path | None = None):
        self.file = Path(file) if file is not None else default_settings_path().with_name("connections.json")
        if home is None:
            from desktop.core.harness import skills
            home = skills.USER_HOME or Path.home()
        self.home = Path(home)
        self._preview: dict[str, list[dict]] = {}

    def _load(self):
        try:
            data = _read(self.file)
            entries = data.get("entries", [])
            if not isinstance(entries, list):
                return []
            return [dict(e) for e in entries if isinstance(e, Mapping) and e.get("kind") in KINDS and isinstance(e.get("config"), Mapping) and all(isinstance(e.get(k), str) for k in ("id", "source", "name", "project", "fingerprint")) and isinstance(e.get("enabled"), bool) and not e["source"].startswith(("\\\\", "//"))]
        except ConnectionError:
            return []

    def _save(self, entries):
        if self.file.exists():
            existing = _read(self.file)
            if not isinstance(existing.get("entries"), list):
                raise ConnectionError("已有连接配置损坏，未覆盖原文件。请先备份并修复。")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)

    def _validate(self, entry):
        config = entry["config"]
        if find_secret(json.dumps(config, ensure_ascii=False)):
            raise ConnectionError("配置疑似含密钥，请改为环境变量引用，不要填写密钥正文。")
        if entry["kind"] == "mcp":
            unsupported = sorted(set(config) & {"cwd", "enabled_tools", "disabled_tools", "startup_timeout_sec", "tool_timeout_sec"})
            if unsupported:
                raise ConnectionError("这份 MCP 包含本批尚未支持的选项：" + "、".join(unsupported) + "。未忽略它们或启用服务器。")
            if config.get("url") or not isinstance(config.get("command"), str) or not config["command"].strip():
                raise ConnectionError("本批只支持本地 stdio MCP；远程 MCP／OAuth 暂不支持。")
            if not isinstance(config.get("args", []), list) or any(not isinstance(v, str) for v in config.get("args", [])):
                raise ConnectionError("MCP args 必须是字符串数组。")
            variables = config.get("env_vars", [])
            if not isinstance(variables, list) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in variables):
                raise ConnectionError("MCP env_vars 必须是环境变量名称数组。")
            if not isinstance(config.get("env", {}), Mapping):
                raise ConnectionError("MCP env 必须是环境变量引用表。")
            if any(not isinstance(v, str) or not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", v) for v in config.get("env", {}).values()):
                raise ConnectionError("MCP env 的值只接受 ${变量名}；不保存明文密钥。")
            if entry["name"] == "wittycode-memory":
                raise ConnectionError("这个 MCP 名称由内置共享记忆使用，请换一个名称。")
        elif entry["kind"] == "skill":
            raw = config.get("path")
            if not isinstance(raw, str) or not raw.strip():
                raise ConnectionError("技能配置需要有效的 SKILL.md 路径。")
            file = _local(raw)
            if file.name != "SKILL.md" or not file.is_file():
                raise ConnectionError("技能路径必须指向存在的 SKILL.md 文件。")
            if config.get("pluginRoot") and not file.is_relative_to(_local(config["pluginRoot"])):
                raise ConnectionError("技能路径越出了它所属的插件目录。")
        elif entry["kind"] == "hook":
            if config.get("event") not in HOOK_EVENTS or config.get("type", "command") != "command":
                raise ConnectionError("本地 Hooks 暂支持 SessionStart／SessionEnd／UserPromptSubmit／PreToolUse／PostToolUse／Stop 的 command 处理器。")
            if not isinstance(config.get("command"), str) or not config["command"].strip():
                raise ConnectionError("Hook command 不能为空。")
            timeout = config.get("timeout", 10)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ConnectionError("Hook timeout 必须在 0–120 秒之间。")
            matcher = config.get("matcher", "")
            if not isinstance(matcher, str) or len(matcher) > 500:
                raise ConnectionError("Hook matcher 必须是至多 500 字的正则。")
            try:
                if matcher not in ("", "*"):
                    re.compile(matcher)
            except re.error as error:
                raise ConnectionError("Hook matcher 不是有效正则。") from error

    def _config_entries(self, path, project="", parent="", root=None, *, fingerprint=True):
        path = _local(path)
        raw = _read(path)
        entries = []
        servers = raw.get("mcp_servers") or raw.get("mcpServers") or {}
        if not isinstance(servers, Mapping):
            raise ConnectionError("MCP 配置必须是以服务器名称为键的对象。")
        for name, config in servers.items():
            if not isinstance(config, Mapping):
                continue
            config = dict(config)
            if root is not None:
                config = self._expand(config, root)
            entries.append(_entry("mcp", name, path, config, project, parent, fingerprint=fingerprint))
        hooks = raw.get("hooks", {})
        if isinstance(hooks, Mapping):
            for event, groups in hooks.items():
                if not isinstance(groups, list):
                    continue
                for group_index, group in enumerate(groups):
                    if not isinstance(group, Mapping):
                        continue
                    handlers = group.get("hooks", [])
                    if not isinstance(handlers, list):
                        raise ConnectionError("Hook 分组必须包含 hooks 数组。")
                    for index, handler in enumerate(handlers):
                        if not isinstance(handler, Mapping):
                            continue
                        config = {**handler, "event": event, "matcher": group.get("matcher", "")}
                        if sys.platform == "win32" and config.get("command_windows"):
                            config["command"] = config["command_windows"]
                        if root is not None:
                            config = self._expand(config, root)
                            config["pluginRoot"] = str(root)
                        entries.append(_entry("hook", f"{event} {group_index + 1}.{index + 1}", path, config, project, parent, fingerprint=fingerprint))
        return entries

    @staticmethod
    def _expand(data, root):
        text = json.dumps(data, ensure_ascii=False)
        # JSON encoding preserves Windows backslashes when the manifest is decoded again.
        for name in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "DROID_PLUGIN_ROOT"):
            text = text.replace("${" + name + "}", json.dumps(str(root))[1:-1])
        return json.loads(text)

    def _from_path(self, path, project="", *, fingerprint=True):
        path = _local(path)
        if not path.exists():
            raise ConnectionError("找不到这个本地配置文件或插件目录。")
        if path.is_file() and path.name == "plugin.json" and path.parent.name in (".codex-plugin", ".claude-plugin", ".factory-plugin"):
            return self._from_path(path.parent.parent, project, fingerprint=fingerprint)
        if path.is_file() and path.name == "SKILL.md":
            return [_entry("skill", path.parent.name, path, {"path": str(path)}, project, fingerprint=fingerprint)]
        if path.is_file():
            return self._config_entries(path, project, fingerprint=fingerprint)
        manifest = next((path / folder / "plugin.json" for folder in (".codex-plugin", ".claude-plugin", ".factory-plugin") if (path / folder / "plugin.json").is_file()), None)
        if manifest is None:
            if (path / "SKILL.md").is_file():
                return self._from_path(path / "SKILL.md", project, fingerprint=fingerprint)
            raise ConnectionError("插件目录需要 .codex-plugin/plugin.json；技能目录需要 SKILL.md。")
        raw = _read(_local(manifest))
        plugin = _entry("plugin", raw.get("name") or path.name, path, {"root": str(path), "manifest": str(manifest)}, project, fingerprint=fingerprint)
        entries = [plugin]
        roots = raw.get("skills", "./skills")
        roots = [roots] if isinstance(roots, str) else roots
        if not isinstance(roots, list):
            raise ConnectionError("插件 skills 必须是相对目录或目录数组。")
        for value in roots:
            folder = _safe_member(path, value)
            for child in sorted(folder.iterdir()) if folder.is_dir() else []:
                child = _local(child)
                if not child.is_dir():
                    continue
                file = _local(child / "SKILL.md")
                if file.is_file():
                    entries.append(_entry("skill", file.parent.name, file, {"path": str(file), "pluginRoot": str(path)}, project, plugin["id"], fingerprint=fingerprint))
        for key, default in (("hooks", "./hooks/hooks.json"), ("mcpServers", "./.mcp.json")):
            specs = raw.get(key, default)
            specs = specs if isinstance(specs, list) else [specs]
            for spec in specs:
                if isinstance(spec, str):
                    file = _safe_member(path, spec)
                    if file.is_file():
                        entries += self._config_entries(file, project, plugin["id"], path, fingerprint=fingerprint)
                elif isinstance(spec, Mapping):
                    # Inline lifecycle/MCP objects are parsed by the same path-independent parser.
                    entries += self._inline_entries(manifest, key, spec, project, plugin["id"], path, fingerprint=fingerprint)
        return list({e["id"]: e for e in entries}.values())

    def _inline_entries(self, source, key, data, project, parent, root, *, fingerprint=True):
        entries = []
        if key == "mcpServers":
            for name, config in data.items():
                if isinstance(config, Mapping):
                    entries.append(_entry("mcp", name, source, self._expand(config, root), project, parent, fingerprint=fingerprint))
        else:
            hooks = data.get("hooks", data)
            if not isinstance(hooks, Mapping):
                raise ConnectionError("插件 hooks 必须是事件配置对象。")
            for event, groups in hooks.items():
                if not isinstance(groups, list):
                    continue
                for i, group in enumerate(groups):
                    if not isinstance(group, Mapping) or not isinstance(group.get("hooks", []), list):
                        raise ConnectionError("插件 Hook 分组必须包含 hooks 数组。")
                    for j, handler in enumerate(group.get("hooks", [])):
                        if not isinstance(handler, Mapping):
                            raise ConnectionError("插件 Hook 处理器必须是对象。")
                        config = self._expand({**handler, "event": event, "matcher": group.get("matcher", ""), "pluginRoot": str(root)}, root)
                        entries.append(_entry("hook", f"{event} {i + 1}.{j + 1}", source, config, project, parent, fingerprint=fingerprint))
        return entries

    def preview(self, path, project=""):
        entries = self._from_path(path, project)
        if not entries:
            raise ConnectionError("没有找到可导入的 Skill、Plugin、Hook 或 MCP 配置。")
        for entry in entries:
            self._validate(entry)
        key = _digest(entries)
        self._preview[key] = entries
        return {"preview": key, "items": entries, "commands": [{"name": e["name"], "kind": e["kind"], "command": e["config"].get("command", ""), "args": e["config"].get("args", [])} for e in entries if e["kind"] in ("hook", "mcp")]}

    def commit(self, key, confirmed):
        if not isinstance(key, str) or not key:
            raise ConnectionError("导入预览标识不合法，请重新预览。")
        if confirmed is not True:
            raise ConnectionError("请先确认本次导入包含的可执行命令。")
        entries = self._preview.get(key)
        if entries is None:
            raise ConnectionError("导入预览已过期，请重新预览。")
        for entry in entries:
            if _source_hash(Path(entry["source"])) != entry["fingerprint"]:
                raise ConnectionError("预览后源文件发生变化，请重新预览和确认。")
        old = {entry["id"]: entry for entry in self._load()}
        old.update({entry["id"]: {**entry, "enabled": True, "imported": True} for entry in entries})
        self._save(list(old.values()))
        self._preview.pop(key, None)
        return {"ok": True, "count": len(entries)}

    def scan(self, project=""):
        from desktop.core.harness import skills

        stored = {e["id"]: e for e in self._load() if not e.get("project") or e["project"] == str(project)}
        detected, errors = [], []
        for skill in skills.discover(project or self.home, self.home):
            try:
                entry = _entry("skill", skill.name, skill.path, {"path": str(skill.path)}, project if skill.source == "project" else "", fingerprint=False)
                entry["enabled"] = True
                detected.append(entry)
            except (OSError, ConnectionError):
                errors.append({"source": str(skill.path), "text": "无法读取这个技能来源，请检查本地路径、目录链接和权限。"})
        roots = [(codex_home(self.home), "")]
        if project:
            roots += [(Path(project) / ".codex", str(project)), (Path(project) / ".wittycode", str(project))]
        seen_roots = set()
        for root, scope in roots:
            try:
                root = _local(root)
            except ConnectionError as error:
                errors.append({"source": str(root), "text": str(error)})
                continue
            key = os.path.normcase(str(root))
            if key in seen_roots:
                continue
            seen_roots.add(key)
            for name in ("config.toml", "hooks.json", ".mcp.json"):
                file = root / name
                try:
                    file = _local(file)
                    if file.is_file():
                        detected += self._config_entries(file, scope, fingerprint=False)
                except ConnectionError as error:
                    errors.append({"source": str(file), "text": str(error)})
                except OSError:
                    errors.append({"source": str(file), "text": "连接配置不可访问，请检查文件权限。"})
            plugins = root / "plugins"
            try:
                plugins = _local(plugins)
                children = sorted(plugins.iterdir()) if plugins.is_dir() else []
                for child in children:
                    manifest = child / ".codex-plugin/plugin.json"
                    try:
                        # Cache versions, marketplace checkouts and temporary downloads are not an installation list.
                        if child.name.startswith(".") or child.name in {"cache", "marketplaces", "node_modules", "__pycache__"} or _linked(child):
                            continue
                        if child.is_dir() and manifest.is_file():
                            detected += self._from_path(child, scope, fingerprint=False)
                    except ConnectionError as error:
                        errors.append({"source": str(manifest), "text": str(error)})
                    except OSError:
                        errors.append({"source": str(manifest), "text": "插件配置不可访问，请检查文件权限。"})
            except ConnectionError as error:
                errors.append({"source": str(plugins), "text": str(error)})
            except OSError:
                errors.append({"source": str(plugins), "text": "插件目录不可访问，请检查文件权限。"})
        managed_skills = {os.path.normcase(entry["source"]) for entry in stored.values() if entry["kind"] == "skill"}
        for entry in detected:
            if entry["kind"] == "skill" and os.path.normcase(entry["source"]) in managed_skills:
                continue
            try:
                self._validate(entry)
            except ConnectionError as error:
                errors.append({"source": entry["source"], "text": str(error)})
                continue
            stored.setdefault(entry["id"], entry)
        items = []
        seen_items = set()
        for entry in stored.values():
            identity = (entry["kind"], os.path.normcase(entry["source"]), "" if entry["kind"] == "skill" else entry["name"], os.path.normcase(entry["project"]))
            if identity in seen_items:
                continue
            seen_items.add(identity)
            entry = dict(entry)
            try:
                entry["changed"] = entry.get("imported", False) and _source_hash(Path(entry["source"])) != entry["fingerprint"]
            except (OSError, ConnectionError):
                entry["changed"] = True
            items.append(entry)
        unique_errors = {(os.path.normcase(error["source"]), error["text"]): error for error in errors}
        return {"items": items, "errors": list(unique_errors.values()), "hookEvents": sorted(HOOK_EVENTS)}

    def update(self, entry_id, *, enabled=None, config=None, confirmed=False, project=""):
        stored = {e["id"]: e for e in self._load()}
        entry = stored.get(entry_id)
        if entry is None:
            entry = next((e for e in self.scan(project)["items"] if e["id"] == entry_id), None)
        if entry is None:
            raise ConnectionError("找不到这个连接。")
        entry = dict(entry)
        if entry.get("project") and entry["project"] != str(project):
            raise ConnectionError("这个连接属于另一个项目。")
        if config is not None:
            if not isinstance(config, Mapping):
                raise ConnectionError("连接配置必须是 JSON 对象。")
            entry["config"] = dict(config)
        if entry["kind"] in ("hook", "mcp", "plugin") and (config is not None or (enabled is True and not entry.get("imported"))) and not confirmed:
            raise ConnectionError("启用或更改可执行配置前，需要确认命令。")
        self._validate(entry)
        if entry["kind"] == "skill" and config is not None:
            file = _local(entry["config"]["path"])
            entry["config"]["path"] = entry["source"] = str(file)
            entry["name"] = file.parent.name
        if entry["kind"] == "plugin" and config is not None:
            raise ConnectionError("请用导入预览更新插件，以便一次确认它包含的全部命令。")
        if entry["kind"] != "skill" and (enabled is True or config is not None) and _source_hash(Path(entry["source"])) != entry["fingerprint"]:
            raise ConnectionError("连接来源已经变化，请重新预览导入并确认。")
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ConnectionError("启用状态必须是布尔值。")
            entry["enabled"] = enabled
        if config is not None or enabled is True:
            entry["imported"] = True
            entry["fingerprint"] = _source_hash(Path(entry["source"]))
        stored[entry_id] = entry
        self._save(list(stored.values()))
        return {"ok": True}

    def active(self, kind, project):
        entries = self._load()
        by_id = {e["id"]: e for e in entries}
        hashes = {}
        def allowed(entry, trail=frozenset()):
            if entry["id"] in trail:
                return False
            if not entry.get("enabled") or not entry.get("imported") or (entry.get("project") and entry["project"] != str(project)):
                return False
            source = entry["source"]
            if source not in hashes:
                hashes[source] = _source_hash(Path(source))
            if hashes[source] != entry["fingerprint"]:
                return False
            parent = by_id.get(entry.get("parent"))
            return False if entry.get("parent") and parent is None else allowed(parent, trail | {entry["id"]}) if parent is not None else True
        kept = []
        for entry in entries:
            if entry["kind"] == kind:
                try:
                    self._validate(entry)
                    if allowed(entry):
                        kept.append(entry)
                except (OSError, ConnectionError):
                    continue
        return kept

    def skill_allowed(self, path, project):
        matches = [e for e in self._load() if e["kind"] == "skill" and e["config"].get("path") == str(path)]
        return not matches or any(e["id"] in {item["id"] for item in self.active("skill", project)} for e in matches)

    def skill_sources(self, project):
        from desktop.core.harness.skills import Source
        return [Source("plugin" if e.get("parent") else "personal", Path(e["config"]["path"]).parent, Path(e["config"]["pluginRoot"]) if e["config"].get("pluginRoot") else None) for e in self.active("skill", project)]

    def mcp_commands(self, project):
        selected = {}
        for entry in self.active("mcp", project):
            if entry["name"] not in selected or entry.get("project") or not selected[entry["name"]].get("project"):
                selected[entry["name"]] = entry
        return [self._mcp_command(entry) for entry in selected.values()]

    @staticmethod
    def _mcp_command(entry):
        config = entry["config"]
        env = {}
        for name, reference in config.get("env", {}).items():
            variable = reference[2:-1]
            if variable not in os.environ:
                raise ConnectionError(f"MCP 环境变量 {variable} 尚未设置。")
            env[name] = os.environ[variable]
        for name in config.get("env_vars", []):
            if name in os.environ:
                env[name] = os.environ[name]
        return McpCommand(entry["name"], config["command"], tuple(config.get("args", [])), env)

    async def test(self, entry_id, project):
        entry = next((e for e in self.active("mcp", project) if e["id"] == entry_id), None)
        if entry is None:
            raise ConnectionError("请先导入并确认启用这个 MCP，再测试连接。")
        command = self._mcp_command(entry)
        client = McpClient(command.name, command.command, command.args, command.env, cwd=str(project or Path(entry["source"]).parent))
        try:
            await client.start(timeout=15)
            return {"ok": True, "text": f"连接成功 · {len(client.tools)} 个工具", "tools": [t.name for t in client.tools]}
        except Exception as error:
            causes = []
            while error is not None and error not in causes:
                causes.append(error)
                error = error.__cause__
            if any(isinstance(cause, FileNotFoundError) for cause in causes):
                text = "MCP 连接失败：找不到启动程序，请先安装依赖或填写可执行文件的完整路径。"
            elif any(isinstance(cause, PermissionError) for cause in causes):
                text = "MCP 连接失败：没有启动程序或访问工作目录的权限。"
            elif any(isinstance(cause, TimeoutError) for cause in causes):
                text = "MCP 连接失败：服务器握手或工具列表读取超时，请检查本地依赖与运行环境。"
            elif any("the server exited" in str(cause) for cause in causes):
                text = "MCP 连接失败：服务器在握手或读取工具列表时提前退出，请检查程序依赖与运行环境。"
            else:
                text = "MCP 连接失败：未能完成握手或读取工具列表，请检查命令、依赖和环境变量。"
            # Server output and raw exception text can contain credentials; never send them to the page.
            return {"ok": False, "text": text, "tools": []}
        finally:
            await client.close()

    async def hooks(self, event, project, payload=None):
        payload = {"hook_event_name": event, "cwd": str(project), **(payload or {})}
        native_tool = payload.get("tool_name", "")
        aliases = {"shell": ("Bash", "exec_command"), "create_file": ("apply_patch", "Write"), "edit_file": ("apply_patch", "Edit"), "read_file": ("Read",), "list_dir": ("LS",), "grep": ("Grep",), "glob": ("Glob",)}.get(native_tool, ())
        if aliases:
            payload["wittycode_tool_name"] = native_tool
            payload["tool_name"] = aliases[0]
        contexts = []
        for entry in self.active("hook", project):
            config = entry["config"]
            if config.get("event") != event:
                continue
            matcher = config.get("matcher", "")
            names = [str(native_tool or payload.get("source") or ""), *aliases]
            if matcher not in ("", "*") and not any(re.search(matcher, name) for name in names):
                continue
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            if config.get("pluginRoot"):
                env.update(PLUGIN_ROOT=config["pluginRoot"], CLAUDE_PLUGIN_ROOT=config["pluginRoot"])
            process = await asyncio.create_subprocess_exec(str(SYSTEM32 / "WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", config["command"], cwd=project, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")), config.get("timeout", 10))
            except (TimeoutError, asyncio.CancelledError):
                # This process and its descendants were created by this hook, never another application.
                if process.returncode is None:
                    killer = await asyncio.create_subprocess_exec(str(SYSTEM32 / "taskkill.exe"), "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
                    await killer.wait()
                await process.wait()
                if asyncio.current_task().cancelling():
                    raise
                raise ConnectionError("Hook 超时，已停止本次 Hook；没有继续执行工具。")
            text = stdout[:60_000].decode("utf-8-sig", "replace").strip()
            try:
                result = json.loads(text) if text else {}
            except ValueError:
                result = {}
            result = result if isinstance(result, Mapping) else {}
            specific = result.get("hookSpecificOutput", {})
            specific = specific if isinstance(specific, Mapping) else {}
            if process.returncode == 2 or result.get("continue") is False or result.get("decision") == "block" or specific.get("permissionDecision") == "deny":
                raise ConnectionError("操作被已启用的 Hook 拒绝。")
            if process.returncode:
                raise ConnectionError("Hook 执行失败，没有继续执行工具。")
            context = specific.get("additionalContext") or result.get("additionalContext")
            if isinstance(context, str):
                contexts.append(context[:12_000])
            elif event in ("SessionStart", "UserPromptSubmit") and text and not result:
                contexts.append(text[:12_000])
        return "\n".join(contexts)
