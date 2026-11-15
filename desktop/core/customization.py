"""WittyCode-owned global rules and explicitly selected project AGENTS.md editors."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile

from desktop.core.hooks import PromptContext, PromptEvent
from desktop.core.settings import default_settings_path

MAX_RULE_BYTES = 32 * 1024
_USED_PROFILES: set[str] = set()


class RulesError(ValueError):
    pass


def global_rules_path() -> Path:
    return default_settings_path().parent / "customization" / "AGENTS.md"


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if str(path).startswith(("\\\\", "//")):
        raise RulesError("规则文件必须位于本机。")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RulesError("规则文件路径含链接，未读写。")
        except FileNotFoundError:
            continue
    return path


def read_rules(path: Path) -> dict:
    path = _safe(path)
    exists = path.exists()
    if exists and (not path.is_file() or path.stat().st_size > MAX_RULE_BYTES):
        raise RulesError("规则文件超过 32 KiB，或不是普通文件。")
    raw = path.read_bytes() if exists else b""
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeError as error:
        raise RulesError("规则文件不是 UTF-8 或带 BOM 的 UTF-16；未转换原文件。") from error
    revision = hashlib.sha256(bytes([exists]) + raw).hexdigest()
    return {"path": str(path), "text": text, "revision": revision, "exists": exists}


def save_rules(path: Path, text: str, revision: str) -> dict:
    if not isinstance(text, str) or not isinstance(revision, str) or len(text.encode("utf-8")) > MAX_RULE_BYTES:
        raise RulesError("规则内容不能超过 32 KiB。")
    current = read_rules(path)
    if current["revision"] != revision:
        raise RulesError("文件已被其他程序修改，请重新加载后再保存。")
    path = _safe(path)
    raw = path.read_bytes() if current["exists"] else b""
    encoding = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be" if raw.startswith(b"\xfe\xff") else "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    if "\r\n" in raw.decode(encoding):
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode(encoding)
    if encoding.startswith("utf-16-"):
        data = raw[:2] + data
    if len(data) > MAX_RULE_BYTES:
        raise RulesError("保存后的规则文件不能超过 32 KiB。")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rules-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        if read_rules(path)["revision"] != revision:
            raise RulesError("文件已被其他程序修改，请重新加载后再保存。")
        os.replace(temporary, _safe(path))
    finally:
        Path(temporary).unlink(missing_ok=True)
    return read_rules(path)


def global_instructions() -> str:
    try:
        return read_rules(global_rules_path())["text"].strip()
    except (OSError, RulesError):
        return ""


def prompt_hook(event: PromptEvent) -> PromptContext | None:
    if event.engine == "chatgpt":
        return None
    from desktop.core.harness.prompt import load_agents
    texts = [global_instructions()]
    if event.project:
        texts.extend(item.text for item in load_agents(Path(event.project)).files)
    text = "\n\n".join(part for part in texts if part.strip())
    profile = str(global_rules_path().parent)
    used = profile in _USED_PROFILES
    try:
        marker = _safe(global_rules_path().with_name("external-rules.used"))
        used = used or marker.is_file()
        if text and not used:
            marker.parent.mkdir(parents=True, exist_ok=True)
            with marker.open("xb") as file:
                file.write(b"1\n")
    except (OSError, RulesError):
        pass  # a read-only settings folder must not prevent the rules themselves being supplied
    if text:
        _USED_PROFILES.add(profile)
    elif not used:
        return None  # no extra context for users who have never supplied custom rules
    # External CLIs own their system prompts: carry the current rules, including removal, just like the language hook.
    # The own loop instead keeps them in its sectioned instruction snapshot, without repeating unchanged text.
    return PromptContext("Current WittyCode custom instructions replace earlier WittyCode custom instructions. Project rules override global rules.\n" + (text or "No WittyCode custom instructions are currently configured."))
