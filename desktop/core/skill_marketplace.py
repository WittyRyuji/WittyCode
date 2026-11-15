"""Install public skills from the two official repositories, with pinned commits and explicit consent."""

from __future__ import annotations

import io
import asyncio
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import time
import threading
from contextlib import suppress
import zipfile

from desktop.core.connections import ConnectionError, ConnectionStore, _local, _source_hash
from desktop.core.harness.skills import split_front_matter
from desktop.core.plugin_http import PluginError, PluginHttp, checked_json

SOURCES = {"openai": ("openai/skills", "skills/.curated"), "anthropic": ("anthropics/skills", "skills")}
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}")


class SkillMarketplace:
    def __init__(self, connections: ConnectionStore, *, http=None):
        self.connections = connections
        self.root = connections.file.parent / "installed-skills"
        self.http = http or PluginHttp()
        self.previews = {}
        self.catalogs = {}
        self.closed = False
        self._lock = threading.RLock()

    def _target(self, source, name):
        if source not in SOURCES or not isinstance(name, str) or not NAME.fullmatch(name):
            raise PluginError("技能来源或名称不合法。")
        return _local(self.root / source / name)

    async def catalog(self, source, *, refresh=False):
        if source not in SOURCES:
            raise PluginError("技能商店只提供 OpenAI 和 Anthropic 官方公开仓库。")
        cached = self.catalogs.get(source)
        cache_file = self.root.parent / "skill-catalog" / (source + ".json")
        if cached is None:
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("items"), list) and all(isinstance(i, dict) and NAME.fullmatch(i.get("name", "")) and SHA.fullmatch(i.get("commit", "")) and i.get("source") == source for i in raw["items"]):
                    cached = raw
            except (OSError, ValueError, TypeError):
                pass
        warning = ""
        if refresh or cached is None or time.time() - cached.get("fetchedAt", 0) > 86400:
            repo, folder = SOURCES[source]
            try:
                commit = checked_json(await self.http.request("GET", f"https://api.github.com/repos/{repo}/commits/main"))
                revision, tree_sha = commit["sha"], commit["commit"]["tree"]["sha"]
                if not SHA.fullmatch(revision) or not SHA.fullmatch(tree_sha):
                    raise PluginError("官方仓库返回的版本标识不合法。")
                tree = checked_json(await self.http.request("GET", f"https://api.github.com/repos/{repo}/git/trees/{tree_sha}?recursive=1"))
                if tree.get("truncated") or not isinstance(tree.get("tree"), list):
                    raise PluginError("官方仓库目录不完整，请稍后重试。")
                names = []
                for entry in tree["tree"]:
                    path = str(entry.get("path", ""))
                    if entry.get("type") == "blob" and path.startswith(folder + "/") and path.endswith("/SKILL.md"):
                        name = path[len(folder) + 1:-len("/SKILL.md")]
                        if NAME.fullmatch(name):
                            names.append(name)
                cached = {"fetchedAt": time.time(), "items": [{"name": name, "source": source, "commit": revision, "url": f"https://github.com/{repo}/tree/{revision}/{folder}/{name}"} for name in sorted(set(names), key=str.casefold)]}
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(cached), encoding="utf-8")
                temporary.replace(cache_file)
            except (PluginError, KeyError, TypeError, OSError, ValueError) as error:
                if cached is None:
                    raise PluginError("无法读取官方技能目录，请检查网络或 GitHub 访问限制。") from error
                warning = "正在显示上次成功读取的技能目录。"
        repo, folder = SOURCES[source]
        cached["items"] = [{"source": source, "name": i["name"], "commit": i["commit"], "url": f"https://github.com/{repo}/tree/{i['commit']}/{folder}/{i['name']}"} for i in cached["items"]]
        self.catalogs[source] = cached
        return {"items": [{**item, "installed": (self._target(source, item["name"]) / "SKILL.md").is_file()} for item in cached["items"]], "warning": warning}

    async def preview(self, source, name):
        if self.closed:
            raise PluginError("应用正在关闭，已取消技能下载。")
        target = self._target(source, name)
        if target.exists():
            raise PluginError("这个技能目录已经存在，未覆盖。")
        catalog = await self.catalog(source)
        item = next((i for i in catalog["items"] if i["name"] == name), None)
        if item is None:
            raise PluginError("官方目录中没有这个技能。")
        repo = SOURCES[source][0]
        response = await self.http.request("GET", f"https://codeload.github.com/{repo}/zip/{item['commit']}", limit=80_000_000)
        if self.closed:
            raise PluginError("应用正在关闭，已取消技能下载。")
        if response.status != 200:
            raise PluginError("技能下载失败，请检查网络或仓库访问限制。")
        return await asyncio.to_thread(self._prepare, source, name, item, response.body)

    def _prepare(self, source, name, item, body):
        target = self._target(source, name)
        if target.exists():
            raise PluginError("这个技能目录已经存在，未覆盖。")
        folder = SOURCES[source][1]
        token = secrets.token_hex(16)
        staging = _local(self.root.parent / "skill-downloads" / token)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                count, total = 0, 0
                for member in archive.infolist():
                    parts = PurePosixPath(member.filename).parts
                    if len(parts) < 2:
                        continue
                    relative = PurePosixPath(*parts[1:])
                    prefix = PurePosixPath(folder) / name
                    is_skill = relative.is_relative_to(prefix)
                    is_license = len(relative.parts) == 1 and relative.name.lower().startswith(("license", "copying", "notice"))
                    if not is_skill and not is_license:
                        continue
                    if "\\" in member.orig_filename or any(ord(c) < 32 for c in member.orig_filename) or PurePosixPath(member.filename).is_absolute() or any(":" in p or p in (".", "..") or p.rstrip(". ") != p for p in parts) or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                        raise PluginError("技能归档含越界路径、链接或加密项，未安装。")
                    if member.is_dir():
                        continue
                    count += 1; total += member.file_size
                    if count > 1000 or total > 12_000_000:
                        raise PluginError("技能超过安装上限（1000 个文件／12 MB）。")
                    output = staging / (Path(*relative.relative_to(prefix).parts) if is_skill else Path("repository-" + relative.name))
                    if output.exists():
                        raise PluginError("技能归档包含重复文件，未安装。")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(archive.read(member))
            skill_file = staging / "SKILL.md"
            if not skill_file.is_file():
                raise PluginError("归档中没有这个技能的 SKILL.md。")
            text = skill_file.read_text(encoding="utf-8-sig")
            meta, _ = split_front_matter(text)
            if not meta.get("name") or not meta.get("description"):
                raise PluginError("这个技能缺少 name 或 description。")
            license_files = sorted(p.name for p in staging.iterdir() if p.is_file() and any(word in p.name.lower() for word in ("license", "copying", "notice")))
            license_text = "\n\n".join(p.read_text(encoding="utf-8", errors="replace")[:20_000] for p in staging.iterdir() if p.name in license_files)
            fingerprint = _source_hash(staging)
            self._register(token, {"path": staging, "target": target, "item": item, "hash": fingerprint})
            return {"preview": token, "name": name, "source": source, "commit": item["commit"], "url": item["url"], "description": str(meta["description"])[:2000], "license": str(meta.get("license") or "请查看随附许可和官方来源；不同技能的许可可能不同。"), "licenseText": license_text, "files": count}
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
            shutil.rmtree(staging)
            if isinstance(error, PluginError):
                raise
            raise PluginError("下载的技能内容无效，未安装。") from error

    @staticmethod
    def _discard(record):
        path = _local(record["path"])
        if path.exists():
            shutil.rmtree(path)

    def _register(self, token, record):
        stale = []
        with self._lock:
            if self.closed:
                raise PluginError("应用正在关闭，已取消技能下载。")
            while len(self.previews) >= 5:
                oldest = next(iter(self.previews))
                stale.append(self.previews.pop(oldest))
            self.previews[token] = record
        for old in stale:
            with suppress(OSError, ConnectionError):
                self._discard(old)

    def install(self, preview, *, confirmed=False):
        if self.closed or not isinstance(preview, str) or confirmed is not True:
            raise PluginError("请先预览并确认技能来源、许可和可执行内容。")
        with self._lock:
            record = self.previews.pop(preview, None)
        if record is None:
            raise PluginError("安装预览已过期，请重新预览并确认。")
        try:
            staging, target = _local(record["path"]), _local(record["target"])
            if target.exists() or _source_hash(staging) != record["hash"]:
                raise PluginError("安装目标已存在或预览内容发生变化，未覆盖。")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(target)
            try:
                imported = self.connections.preview(str(target / "SKILL.md"), "")
                self.connections.commit(imported["preview"], True)
            except BaseException:
                target.rename(staging)
                raise
            return {"ok": True, "path": str(target), "source": record["item"]["url"]}
        except BaseException:
            if not self.closed:
                self._register(preview, record)
            else:
                with suppress(OSError, ConnectionError):
                    self._discard(record)
            raise

    def close(self):
        with self._lock:
            self.closed = True
            records = list(self.previews.values())
            self.previews.clear()
        for record in records:
            with suppress(OSError, ConnectionError):
                self._discard(record)

    def cancel(self, preview):
        with self._lock:
            record = self.previews.pop(preview, None) if isinstance(preview, str) else None
        if record:
            self._discard(record)
        return {"ok": True}
