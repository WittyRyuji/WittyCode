"""API providers: the ``customModels`` list of WittyCode's own models file (``models.json``), as the settings page shows it.

Nothing here imports Qt. The page talks to ``ProviderStore`` through the bridge; every value that crosses is JSON-shaped
with the camelCase keys of ``frontend/src/types.ts`` (``ApiProvider``, ``ProviderDraft``, ``ApiModel``) and no returned
value ever holds a secret (a legacy plaintext key is only visible as a masked tail).

What the models file holds (``models.json`` -> ``customModels``, an array of objects)
------------------------------------------------------------------------------------
The common "bring your own key" format of agent applications. WittyCode reads and writes only its own file, next to its
settings; it never reads another program's settings.

Fields of one entry (``model`` is the only one that is always there):

- ``model`` (str): the id sent to the API. ``displayName`` (str): the name in the model menu; ``model`` when it is
  missing. ``baseUrl`` (str, http/https): required unless the entry has a ``bedrock`` block.
- ``provider``: exactly ``anthropic`` | ``openai`` | ``generic-chat-completion-api`` | ``bedrock-converse``.
  ``bedrock-converse`` requires a ``bedrock`` block; a ``bedrock`` block also goes with ``anthropic`` (InvokeModel /
  Messages) and ``openai`` (Bedrock's OpenAI endpoint), never with ``generic-chat-completion-api``.
- ``apiKey`` (str, optional, never empty): ``${VAR_NAME}`` is expanded from the environment (also in ``extraHeaders``
  values and in ``bedrock.awsRegion`` / ``awsProfile`` / ``bedrockBaseUrl`` / ``requestMetadata``); an unset variable is
  an error naming it. The literal placeholders ``YOUR_API_KEY`` and ``YOUR_OPENAI_API_KEY`` make the entry unusable.
  Omit it for keyless endpoints.
- ``apiKeyHelper`` (str, optional): a shell command whose trimmed stdout is the key, run from the home directory; it wins
  over ``apiKey``. ``apiKeyHelperTtlMs`` (positive int, default 300000 = 5 min).
- ``authMode``: ``bearer`` (send ``Authorization: Bearer`` instead of ``x-api-key``; only valid with
  ``provider: anthropic`` and no ``bedrock``) or ``provider-default``.
- ``maxOutputTokens``, ``maxContextLimit`` (positive ints), ``reasoningEffort`` (the default level; it also makes the
  menu offer off/minimal/low/medium/high/xhigh/max), ``noImageSupport`` (bool; the default is true for ``generic-chat-completion-api`` and
  false for the other three), ``enableThinking`` / ``thinkingMaxTokens`` (anthropic and bedrock only), ``baseModelId`` /
  ``useInRouter`` (kept as they are), ``extraHeaders`` (object of str; names unique ignoring case), ``extraArgs``
  (object, merged into the request body).
- ``bedrock`` (object): ``authMethod`` (``aws-credentials`` | ``credentials-command`` | ``environment-api-key``;
  ``credentials-command`` needs ``awsCredentialExport``), ``awsRegion``, ``awsProfile``, ``bedrockBaseUrl``,
  ``awsAuthRefresh``, ``awsCredentialExport``, ``requestMetadata``.
- ``id`` (str) and ``index`` (number): optional; WittyCode never writes them and keeps them on entries that have them.

Entries that cannot be used (no ``model``, an unknown ``provider``, neither ``baseUrl`` nor ``bedrock``, a placeholder
key, a broken combination above) are skipped; they are left in the file untouched.

The id of a model
-----------------
``custom:<name>-<n>`` where ``name`` is ``displayName`` (``model`` when there is none) trimmed with every run of
whitespace replaced by one ``-``, and ``n`` is how many *earlier usable entries* have the same ``name`` (0 for the first).
So with distinct display names every id ends in ``-0`` and deleting an entry in the middle changes no other id; only the
later ones of a group of identical names move up. Two older forms are still understood by ``provider_of`` (``n`` = the
0-based position in the array, and ``custom:<model>``), so conversations saved with them keep their model. The rule is in
``custom_model_id``. The page's id is ``api:<provider id>:custom:<name>-<n>``.

How WittyCode keeps its own layer
---------------------------------
``customModels`` is flat. Entries are grouped into providers by ``(provider kind, baseUrl)`` (Bedrock: kind + region).
A sidecar JSON (``providers.json`` next to WittyCode's settings: ``{"version": 1, "providers": [{"id", "name", "kind",
"baseUrl"}]}``, Bedrock records also carry ``region``) remembers only the id and the display name of a group; a group
without a record is an "auto" provider named after its host with an id derived from the key, so it is stable. Only
``customModels`` is ever written to settings.json (read-modify-write, other keys and unknown entry fields kept; the first
write makes one ``settings.json.wittycode-backup``); a group's entries are replaced in place when it already exists.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from desktop.core.llm.aws import Access, AwsError, BedrockSettings, Resolver
from desktop.core.llm.model_specs import known_window
from desktop.core.settings import default_settings_path

log = logging.getLogger(__name__)

MODELS_FILE_ENV = "WITTYCODE_MODELS_FILE"
"""A models file of its own (tests, a second profile) instead of ``%APPDATA%\\WittyCode\\models.json``."""
MODELS_FILE = "models.json"
"""WittyCode's own file of the API providers' model entries: ``{"customModels": [...]}`` in the format described above."""


BACKUP_SUFFIX = ".wittycode-backup"
KINDS = ("anthropic", "openai", "generic-chat-completion-api", "bedrock-converse")
BEDROCK = "bedrock-converse"
EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
BEDROCK_METHODS = ("aws-credentials", "credentials-command", "environment-api-key")
REQUEST_TIMEOUT = 10.0
COMMAND_TIMEOUT = 10.0
AWS_COMMAND_TIMEOUT = 20.0
AWS_LOGIN_TIMEOUT = 180.0
"""A login command waits for the user in a browser."""
DEFAULT_THINKING = 8192
"""The thought budget of an entry with ``enableThinking`` and no ``thinkingMaxTokens``."""
MAX_METADATA = 16
MAX_BODY_BYTES = 1_048_576
CREATE_NO_WINDOW = 0x08000000
BEDROCK_TEST_NOTE = "Bedrock 暂不支持在这里测试连接，保存后在会话里试一下。"

_ENV_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_ENV_ANY = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER_KEYS = ("YOUR_OPENAI_API_KEY", "YOUR_API_KEY")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SECRET_HEADER = re.compile(r"authorization|api[-_]?key|token|secret|passw|credential|cookie", re.IGNORECASE)
_AUTH_SCHEME = re.compile(r"^\s*(bearer|basic|token|digest)\s+", re.IGNORECASE)
_BULLETS = "\u2022" * 8

# JavaScript's \s, which is what the name rule uses; Python's differs at the edges (U+FEFF, U+001C..U+001F, U+0085).
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff" + "".join(chr(c) for c in range(0x2000, 0x200B))
_JS_SPACE_RUN = re.compile("[" + re.escape(_JS_SPACE) + "]+")


class ProviderError(Exception):
    """Something the user can act on; the message is one Chinese sentence."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        # Inference does not follow redirects either. Never forward credentials to a second destination in a probe.
        return None


# ------------------------------------------------------------------------------------------------ the naming rule


def _custom_name(display_name: str) -> str:
    return _JS_SPACE_RUN.sub("-", display_name.strip(_JS_SPACE))


def custom_model_id(display_name: str, ordinal: int) -> str:
    """The id of a custom model: ``ordinal`` counts earlier usable entries with the same sanitized name."""
    return f"custom:{_custom_name(display_name)}-{ordinal}"


# ---------------------------------------------------------------------------------------------- entries and groups


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def _usable(entry: Any) -> bool:
    """Whether the entry can be used (see the module docstring)."""
    if not isinstance(entry, dict) or not _text(entry.get("model")):
        return False
    provider = entry.get("provider")
    bedrock = entry.get("bedrock")
    if provider not in KINDS or entry.get("apiKey") in _PLACEHOLDER_KEYS:
        return False
    if not (_text(entry.get("baseUrl")) or isinstance(bedrock, dict)):
        return False
    if provider == BEDROCK and not isinstance(bedrock, dict):
        return False
    if isinstance(bedrock, dict):
        if provider == "generic-chat-completion-api":
            return False
        if bedrock.get("authMethod") == "credentials-command" and not _text(bedrock.get("awsCredentialExport")):
            return False
    return not (entry.get("authMode") == "bearer" and (provider != "anthropic" or isinstance(bedrock, dict)))


def _norm_url(url: str) -> str:
    text = url.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return text.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}{query}"


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.rsplit("@", 1)[-1]
    except ValueError:
        return ""


def _group_key(entry: dict) -> tuple[str, str]:
    bedrock = entry.get("bedrock")
    if isinstance(bedrock, dict):
        return (BEDROCK, _text(bedrock.get("awsRegion")))
    return (entry["provider"], _norm_url(_text(entry.get("baseUrl"))))


def _auto_id(key: tuple[str, str]) -> str:
    return "auto-" + hashlib.sha1("\0".join(key).encode("utf-8")).hexdigest()[:10]


def _auto_name(key: tuple[str, str], entry: dict) -> str:
    if key[0] == BEDROCK:
        return f"AWS Bedrock \u00b7 {key[1]}" if key[1] else "AWS Bedrock"
    return _host(_text(entry.get("baseUrl"))) or "API Provider"


def _valid_record(record: Any) -> bool:
    return isinstance(record, dict) and bool(_text(record.get("id"))) and isinstance(record.get("name"), str) and record.get("kind") in KINDS


def _record_key(record: dict) -> tuple[str, str]:
    if record["kind"] == BEDROCK:
        return (BEDROCK, _text(record.get("region")))
    return (record["kind"], _norm_url(_text(record.get("baseUrl"))))


@dataclass
class _Group:
    id: str
    name: str
    kind: str
    key: tuple[str, str]
    indices: list[int] = field(default_factory=list)


def _build_groups(entries: Sequence[Any], records: Sequence[dict]) -> list[_Group]:
    by_key: dict[tuple[str, str], dict] = {}
    used: set[str] = set()
    for record in records:
        key = _record_key(record)
        if key not in by_key and record["id"] not in used:
            by_key[key] = record
            used.add(record["id"])
    groups: dict[tuple[str, str], _Group] = {}
    for index, entry in enumerate(entries):
        if not _usable(entry):
            continue
        key = _group_key(entry)
        group = groups.get(key)
        if group is None:
            record = by_key.get(key)
            name = _text(record["name"]) if record else ""
            group = groups[key] = _Group(
                id=record["id"] if record else _auto_id(key),
                name=name or _auto_name(key, entry),
                kind=key[0],
                key=key,
            )
        group.indices.append(index)
    return list(groups.values())


def _display_of(entry: dict) -> str:
    return entry["displayName"] if isinstance(entry.get("displayName"), str) else entry["model"]


def _custom_ids(entries: Sequence[Any]) -> list[tuple[int, str, str]]:
    """(index in the file, the model's id, the id of the older position-based rule) for each usable entry."""
    seen: dict[str, int] = {}
    out: list[tuple[int, str, str]] = []
    for position, (index, entry) in enumerate((i, e) for i, e in enumerate(entries) if _usable(e)):
        display = _display_of(entry)
        name = _custom_name(display)
        ordinal = seen.get(name, 0)
        seen[name] = ordinal + 1
        explicit = entry.get("id") if isinstance(entry.get("id"), str) else None
        out.append((index, explicit if explicit is not None else custom_model_id(display, ordinal), custom_model_id(display, position)))
    return out


# --------------------------------------------------------------------------------------------------- keys and masks


def _mask(secret: str) -> str:
    return secret[:3] + _BULLETS + secret[-4:] if len(secret) >= 12 else _BULLETS


@dataclass(frozen=True)
class _Key:
    mode: str
    hint: str
    ttl: int | None = None
    secret: str | None = field(default=None, repr=False)


_NO_KEY = _Key("none", "")


@dataclass(frozen=True)
class ApiModelSpec:
    """A model of an API provider as WittyCode's own agent loop needs it (``ProviderStore.api_models``)."""

    id: str
    """The application's id for it: ``api:<provider id>:<custom id>``."""
    native: str
    """The id the API knows."""
    display: str
    provider_id: str
    provider_name: str
    kind: str
    """``anthropic`` | ``openai`` | ``generic-chat-completion-api`` | ``bedrock-converse``."""
    base_url: str
    window: int | None
    max_output: int | None
    reasoning: str
    """The level the entry sets (``""``: none, the model has no reasoning control here)."""
    images: bool
    bearer: bool
    headers: tuple[tuple[str, str], ...]
    extra_args: dict
    key: _Key = field(repr=False)
    bedrock: BedrockSettings | None = None
    """The entry's ``bedrock`` block as written (``${VAR}`` not yet filled in): the model is on Amazon Bedrock, whatever its ``kind``."""
    thinking: int | None = None
    """The thought budget of an entry that turned extended thinking on (``enableThinking``, ``thinkingMaxTokens``)."""


@dataclass(frozen=True)
class BedrockPlan:
    """Where and how one request to a Bedrock model goes (``ProviderStore.bedrock_plan``)."""

    region: str
    base_url: str
    """The user's ``bedrockBaseUrl`` (``""``: the regional endpoint)."""
    access: Access = field(repr=False)
    metadata: dict[str, str]


_METADATA_TEXT = re.compile(r"^[\w\s:_@$#=/+,.-]*$")


def _bedrock_settings(block: Any) -> BedrockSettings | None:
    if not isinstance(block, dict):
        return None
    tags = block.get("requestMetadata")
    method = block.get("authMethod")
    return BedrockSettings(
        method=method if method in BEDROCK_METHODS else "",
        region=_text(block.get("awsRegion")),
        profile=_text(block.get("awsProfile")),
        base_url=_text(block.get("bedrockBaseUrl")),
        auth_refresh=_text(block.get("awsAuthRefresh")),
        credential_export=_text(block.get("awsCredentialExport")),
        metadata=tuple((str(k), str(v)) for k, v in tags.items() if isinstance(v, str)) if isinstance(tags, dict) else (),
    )


def _check_metadata(pairs: Sequence[tuple[str, str]]) -> None:
    """AWS's limits on request metadata; exceeding one fails every call of the model, so it is said here with the key."""
    if len(pairs) > MAX_METADATA:
        raise ProviderError(f"requestMetadata 最多 {MAX_METADATA} 项，现在有 {len(pairs)} 项。")
    for key, value in pairs:
        if not 1 <= len(key) <= 256 or len(value) > 256 or not _METADATA_TEXT.match(key) or not _METADATA_TEXT.match(value):
            raise ProviderError(f"requestMetadata 里「{key}」不合 AWS 的限制（键 1–256 个字符、值最多 256 个，只能含字母数字、空白和 : _ @ $ # = / + , . -）。")


def _entry_key(entry: dict) -> _Key:
    helper = _text(entry.get("apiKeyHelper"))
    if helper:
        return _Key("command", helper, _positive_int(entry.get("apiKeyHelperTtlMs")))
    key = entry.get("apiKey")
    if isinstance(key, str) and key.strip():
        if _ENV_REF.match(key.strip()):
            return _Key("env", key.strip())
        return _Key("inline", _mask(key), None, key)
    return _NO_KEY


def _group_key_info(members: Sequence[dict]) -> _Key:
    for entry in members:
        info = _entry_key(entry)
        if info.mode != "none":
            return info
    return _NO_KEY


def _carries_secret(name: str, value: str) -> bool:
    """A header that looks like it carries a credential and has literal text where only ${VAR} references are allowed."""
    if not _SECRET_HEADER.search(name):
        return False
    return bool(_ENV_ANY.sub("", _AUTH_SCHEME.sub("", value)).strip())


def _headers_view(entry: dict) -> list[dict[str, str]]:
    raw = entry.get("extraHeaders")
    if not isinstance(raw, dict):
        return []
    out = []
    for name, value in raw.items():
        text = value if isinstance(value, str) else str(value)
        out.append({"k": str(name), "v": _mask(text) if _carries_secret(str(name), text) else text})
    return out


def _resolve_headers(draft_headers: Sequence[tuple[str, str]], existing: Mapping[str, Any]) -> dict[str, str]:
    known = {str(k).lower(): (str(k), v) for k, v in existing.items() if isinstance(v, str)}
    out: dict[str, str] = {}
    for name, value in draft_headers:
        prior = known.get(name.lower())
        if prior is not None and _carries_secret(*prior) and value == _mask(prior[1]):
            value = prior[1]
        elif _carries_secret(name, value):
            raise ProviderError(f"请求头 {name} 里不能直接写密钥：写成 ${{变量名}}。")
        out[name] = value
    return out


# ------------------------------------------------------------------------------------------------------ views


def _model_view(entry: dict) -> dict[str, Any]:
    display = entry.get("displayName")
    reasoning = entry.get("reasoningEffort")
    no_images = entry.get("noImageSupport")
    if not isinstance(no_images, bool):
        no_images = entry["provider"] == "generic-chat-completion-api"
    view: dict[str, Any] = {"id": entry["model"].strip(), "displayName": display if isinstance(display, str) else ""}
    context = _positive_int(entry.get("maxContextLimit"))
    output = _positive_int(entry.get("maxOutputTokens"))
    # Leave the editable value empty when the bundled specification is being used, so it is not saved as a user override.
    if context:
        view["contextWindow"] = context
    if output:
        view["maxOutput"] = output
    view["reasoning"] = reasoning if reasoning in EFFORTS else ""
    view["images"] = not no_images
    return view


def _provider_view(group: _Group, entries: Sequence[dict]) -> dict[str, Any]:
    members = [entries[i] for i in group.indices]
    first = members[0]
    is_bedrock = group.kind == BEDROCK
    key = _NO_KEY if is_bedrock else _group_key_info(members)
    view: dict[str, Any] = {
        "id": group.id,
        "name": group.name,
        "kind": group.kind,
        "baseUrl": "" if is_bedrock else _text(first.get("baseUrl")),
        "keyMode": key.mode,
        "keyHint": key.hint,
    }
    if key.mode == "command" and key.ttl:
        view["keyTtlMs"] = key.ttl
    if group.kind == "anthropic" and first.get("authMode") == "bearer":
        view["bearer"] = True
    if is_bedrock:
        bedrock = first["bedrock"]
        method = bedrock.get("authMethod")
        view["bedrock"] = {
            "method": method if method in BEDROCK_METHODS else "aws-credentials",
            "region": _text(bedrock.get("awsRegion")),
            "profile": _text(bedrock.get("awsProfile")),
        }
    view["models"] = [_model_view(m) for m in members]
    view["headers"] = _headers_view(first)
    extra = first.get("extraArgs")
    view["extraArgs"] = json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) and extra else ""
    return view


# ------------------------------------------------------------------------------------------------------- drafts


@dataclass
class _Row:
    id: str
    display_name: str
    context: int | None
    max_output: int | None
    reasoning: str
    images: bool


@dataclass
class _Draft:
    id: str
    name: str
    kind: str
    base_url: str
    key_mode: str
    key_hint: str
    ttl: int | None
    bearer: bool
    bedrock: dict[str, str] | None
    headers: list[tuple[str, str]]
    extra_args: dict[str, Any]
    models: list[_Row]

    @property
    def key(self) -> tuple[str, str]:
        if self.kind == BEDROCK:
            return (BEDROCK, (self.bedrock or {}).get("region", ""))
        return (self.kind, _norm_url(self.base_url))


def _optional_count(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    number = _positive_int(value)
    if number is None:
        raise ProviderError(f"{label}要是正整数。")
    return number


def _parse_draft(draft: Any, *, connection_only: bool = False) -> _Draft:
    """Validate a ProviderDraft the way the editor does. ``connection_only`` skips what a connection test does not need."""
    if not isinstance(draft, Mapping):
        raise ProviderError("提交的 Provider 数据格式不对。")
    kind = draft.get("kind")
    if kind not in KINDS:
        raise ProviderError("不支持这种接口类型。")
    name = _text(draft.get("name"))
    if not connection_only and not name:
        raise ProviderError("请先给这个 Provider 起个名字。")

    base_url = ""
    if kind != BEDROCK:
        base_url = _text(draft.get("baseUrl"))
        if not base_url:
            raise ProviderError("Base URL 不能是空的。")
        try:
            parts = urlsplit(base_url)
            valid = bool(re.match(r"^https?://\S+$", base_url) and parts.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("Base URL 要以 http:// 或 https:// 开头。")
        if parts.username or parts.password:
            raise ProviderError("Base URL 里不要带用户名和密码，密钥放在环境变量里。")

    key_mode, key_hint, ttl = "none", "", None
    if kind != BEDROCK:
        key_mode = draft.get("keyMode")
        key_hint = _text(draft.get("keyHint")) if key_mode in ("env", "command") else (draft.get("keyHint") or "")
        if key_mode not in ("env", "command", "none", "inline"):
            raise ProviderError("密钥来源不对。")
        if key_mode == "env" and not _ENV_REF.match(key_hint):
            raise ProviderError("环境变量要写成 ${变量名}：这里填的是变量名，不是密钥本身。")
        if key_mode == "command":
            if not key_hint:
                raise ProviderError("取密钥的命令不能是空的。")
            ttl = _optional_count(draft.get("keyTtlMs"), "刷新间隔")
        if key_mode == "none":
            key_hint = ""
    bearer = bool(draft.get("bearer")) and kind == "anthropic" and key_mode != "none"

    bedrock: dict[str, str] | None = None
    if kind == BEDROCK:
        raw = draft.get("bedrock") if isinstance(draft.get("bedrock"), Mapping) else {}
        method = raw.get("method") or "aws-credentials"
        if method not in BEDROCK_METHODS:
            raise ProviderError("Bedrock 的凭据来源不对。")
        bedrock = {"method": method, "region": _text(raw.get("region")), "profile": _text(raw.get("profile"))}

    headers: list[tuple[str, str]] = []
    seen_headers: set[str] = set()
    for item in draft.get("headers") or []:
        if not isinstance(item, Mapping):
            continue
        header, value = _text(item.get("k")), item.get("v")
        value = value if isinstance(value, str) else ""
        if not header:
            if value.strip():
                raise ProviderError("请求头的名字不能是空的。")
            continue
        if not _HEADER_NAME.match(header):
            raise ProviderError(f"请求头名 {header} 不合法。")
        if "\r" in value or "\n" in value:
            raise ProviderError(f"请求头 {header} 的值里不能有换行。")
        if header.lower() in seen_headers:
            raise ProviderError(f"请求头 {header} 重复了。")
        seen_headers.add(header.lower())
        headers.append((header, value))

    extra_args: dict[str, Any] = {}
    models: list[_Row] = []
    if not connection_only:
        text = draft.get("extraArgs")
        if isinstance(text, str) and text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                raise ProviderError("额外参数不是合法的 JSON。") from None
            if not isinstance(parsed, dict):
                raise ProviderError('额外参数要是一个 JSON 对象，例如 {"temperature": 0.7}。')
            extra_args = parsed
        seen: set[str] = set()
        for item in draft.get("models") or []:
            if not isinstance(item, Mapping):
                continue
            model_id = _text(item.get("id"))
            if not model_id:
                continue
            if model_id in seen:
                raise ProviderError(f"有重复的模型 ID：{model_id}。")
            seen.add(model_id)
            reasoning = item.get("reasoning") or ""
            if reasoning not in ("", *EFFORTS):
                raise ProviderError("推理档位不对。")
            models.append(
                _Row(
                    id=model_id,
                    display_name=_text(item.get("displayName")),
                    context=_optional_count(item.get("contextWindow"), "上下文窗口"),
                    max_output=_optional_count(item.get("maxOutput"), "最大输出"),
                    reasoning=reasoning,
                    images=bool(item.get("images")),
                )
            )
        if not models:
            raise ProviderError("至少要有一个模型 ID。")

    return _Draft(
        id=_text(draft.get("id")) or "p" + uuid.uuid4().hex[:10],
        name=name,
        kind=kind,
        base_url=base_url,
        key_mode=key_mode,
        key_hint=key_hint,
        ttl=ttl,
        bearer=bearer,
        bedrock=bedrock,
        headers=headers,
        extra_args=extra_args,
        models=models,
    )


def _set_or_drop(entry: dict, name: str, value: Any) -> None:
    if value is None:
        entry.pop(name, None)
    else:
        entry[name] = value


def _build_entry(existing: dict | None, draft: _Draft, row: _Row, headers: dict[str, str], inline_secret: str | None) -> dict:
    """One customModels entry for a model row. Fields the editor does not own are kept from ``existing``."""
    entry = copy.deepcopy(existing) if existing is not None else {}
    entry["model"] = row.id
    if row.display_name:
        entry["displayName"] = row.display_name
    elif existing is None or "displayName" in entry:
        entry["displayName"] = row.id
    is_bedrock = draft.kind == BEDROCK
    if is_bedrock:
        entry.pop("baseUrl", None)
        for name in ("apiKey", "apiKeyHelper", "apiKeyHelperTtlMs"):
            entry.pop(name, None)
    else:
        entry["baseUrl"] = draft.base_url
        if draft.key_mode == "env":
            entry["apiKey"] = draft.key_hint
            entry.pop("apiKeyHelper", None)
            entry.pop("apiKeyHelperTtlMs", None)
        elif draft.key_mode == "command":
            entry.pop("apiKey", None)
            entry["apiKeyHelper"] = draft.key_hint
            _set_or_drop(entry, "apiKeyHelperTtlMs", draft.ttl)
        elif draft.key_mode == "inline":
            own = entry.get("apiKey")
            keep = own if isinstance(own, str) and own.strip() and not _ENV_REF.match(own.strip()) else inline_secret
            entry["apiKey"] = keep
        else:
            for name in ("apiKey", "apiKeyHelper", "apiKeyHelperTtlMs"):
                entry.pop(name, None)
    if draft.bearer:
        entry["authMode"] = "bearer"
    elif entry.get("authMode") == "bearer":
        del entry["authMode"]
    if is_bedrock:
        previous = existing.get("provider") if existing is not None and isinstance(existing.get("bedrock"), dict) else None
        entry["provider"] = previous if previous in ("anthropic", "openai", BEDROCK) else BEDROCK
    else:
        entry["provider"] = draft.kind
    _set_or_drop(entry, "maxOutputTokens", row.max_output)
    _set_or_drop(entry, "maxContextLimit", row.context)
    if row.reasoning:
        entry["reasoningEffort"] = row.reasoning
    elif not (isinstance(entry.get("reasoningEffort"), str) and entry["reasoningEffort"] not in EFFORTS):
        entry.pop("reasoningEffort", None)
    default_no_images = entry["provider"] == "generic-chat-completion-api"
    if existing is None or "noImageSupport" in entry or (not row.images) != default_no_images:
        entry["noImageSupport"] = not row.images
    _set_or_drop(entry, "extraHeaders", dict(headers) or None)
    _set_or_drop(entry, "extraArgs", dict(draft.extra_args) or None)
    if is_bedrock and draft.bedrock is not None:
        block = copy.deepcopy(entry["bedrock"]) if isinstance(entry.get("bedrock"), dict) else {}
        implied = "authMethod" not in block and entry.get("bedrock") is not None and draft.bedrock["method"] == "aws-credentials"
        if not implied:
            block["authMethod"] = draft.bedrock["method"]
        _set_or_drop(block, "awsRegion", draft.bedrock["region"] or None)
        _set_or_drop(block, "awsProfile", draft.bedrock["profile"] or None)
        if block.get("authMethod") == "credentials-command" and not _text(block.get("awsCredentialExport")):
            raise ProviderError("Bedrock 的「命令导出凭据」还需要 awsCredentialExport 命令：先在 settings.json 里加上它。")
        entry["bedrock"] = block
    else:
        entry.pop("bedrock", None)
    return entry


# ----------------------------------------------------------------------------------- the environment, commands, HTTP


def _lookup(mapping: Mapping[str, str], name: str) -> str | None:
    value = mapping.get(name)
    if value is None and os.name == "nt":
        folded = name.upper()
        value = next((v for k, v in mapping.items() if isinstance(k, str) and k.upper() == folded), None)
    return value if isinstance(value, str) and value != "" else None


def _registry_environment() -> dict[str, str]:
    """The user's environment variables as stored in HKCU\\Environment: what a program started now would inherit."""
    if os.name != "nt":
        return {}
    import winreg

    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, str):
                    out[name] = value
                index += 1
    except OSError:
        return {}
    return out


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        process.kill()


def run_key_command(command: str, timeout: float = COMMAND_TIMEOUT, what: str = "取密钥的命令", *, need_output: bool = True) -> str:
    """Run an apiKeyHelper (shell, home directory) and return its trimmed stdout. Never echoes output.
    ``what`` names the command in the messages; ``need_output=False`` accepts a command that prints nothing (a login)."""
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            cwd=str(Path.home()),
            **options,
        )
    except OSError:
        raise ProviderError(f"{what}没法运行。") from None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.communicate(timeout=2)
        except subprocess.SubprocessError:
            pass
        raise ProviderError(f"{what} {int(timeout)} 秒内没有返回。") from None
    if process.returncode != 0:
        raise ProviderError(f"{what}失败了（退出码 {process.returncode}）。")
    key = (stdout or "").strip()
    if not key and need_output:
        raise ProviderError(f"{what}没有输出内容。")
    return key


def _model_ids(payload: Any) -> list[str] | None:
    rows = None
    if isinstance(payload, dict):
        rows = next((payload[k] for k in ("data", "models") if isinstance(payload.get(k), list)), None)
    elif isinstance(payload, list):
        rows = payload
    if rows is None:
        return None
    ids: list[str] = []
    for row in rows:
        if isinstance(row, str):
            value = row
        elif isinstance(row, dict):
            value = row.get("id") or row.get("model")
            if not value and isinstance(row.get("name"), str):
                value = row["name"].removeprefix("models/")
        else:
            continue
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    return ids


_WINDOW_KEYS = ("context_window", "context_length", "max_context_length", "max_input_tokens", "inputTokenLimit", "max_model_len", "contextWindow", "context_size", "max_context_window", "maxContextLimit")
_OUTPUT_KEYS = ("max_output_tokens", "max_completion_tokens", "outputTokenLimit", "max_tokens", "maxOutputTokens")
_FIGURE_SOURCES = (None, "top_provider", "capabilities", "limits")
"""Where a server puts the figures: on the entry (Anthropic, Gemini, Together, Mistral, vLLM, LiteLLM) or in a nested object
(OpenRouter's ``top_provider``). OpenAI's own ``/v1/models`` has none of them: its entries are ``id``, ``object``, ``created``,
``owned_by``."""


def _figure(row: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for source in _FIGURE_SOURCES:
        holder = row if source is None else row.get(source)
        if not isinstance(holder, Mapping):
            continue
        for key in keys:
            value = _positive_int(holder.get(key))
            if value:
                return value
    return None


def _model_details(payload: Any) -> list[dict[str, Any]]:
    """What a server's model list says about each model besides its id: ``contextWindow`` and ``maxOutput`` where it gives them.
    Only models that have at least one figure are listed."""
    rows = None
    if isinstance(payload, dict):
        rows = next((payload[k] for k in ("data", "models") if isinstance(payload.get(k), list)), None)
    elif isinstance(payload, list):
        rows = payload
    details: list[dict[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        value = row.get("id") or row.get("model")
        if not value and isinstance(row.get("name"), str):
            value = row["name"].removeprefix("models/")
        if not isinstance(value, str) or not value:
            continue
        window = _figure(row, _WINDOW_KEYS)
        output = _figure(row, _OUTPUT_KEYS)
        if window and output and output >= window:
            output = None  # (a "max_tokens" that is the window itself is not the answer's limit)
        if window or output:
            entry: dict[str, Any] = {"id": value}
            if window:
                entry["contextWindow"] = window
            if output:
                entry["maxOutput"] = output
            if all(entry["id"] != seen["id"] for seen in details):
                details.append(entry)
    return details


def _models_url(draft: _Draft) -> str:
    base = draft.base_url.rstrip("/")
    if draft.kind == "anthropic":
        return base + ("/models" if base.endswith("/v1") else "/v1/models")
    return base + "/models"


def _http_problem(code: int, reason: str) -> str:
    if code == 401:
        return "401：密钥无效"
    if code == 403:
        return "403：没有权限，密钥无效或没有开通这个接口"
    if code == 404:
        return "404：没有找到 /models 接口，Base URL 可能不对"
    if code == 429:
        return "429：请求太频繁，或者额度用完了"
    if code >= 500:
        return f"{code}：服务端出错了"
    return f"{code}：{reason.strip()}" if reason.strip() else str(code)


def _network_problem(host: str, error: BaseException) -> str:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return f"连不上 {host}：{int(REQUEST_TIMEOUT)} 秒内没有响应"
    if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
        return f"连不上 {host}：服务没有运行？"
    if isinstance(reason, ssl.SSLError):
        return f"{host} 的 HTTPS 证书没有通过验证"
    if isinstance(reason, socket.gaierror):
        return f"连不上 {host}：找不到这个域名"
    return f"连不上 {host}：网络错误（{type(reason).__name__}）"


def _fail(text: str) -> dict[str, Any]:
    return {"ok": False, "text": text.rstrip("。"), "ms": None, "models": None}


# ------------------------------------------------------------------------------------------------------- the store


@dataclass
class _State:
    doc: dict
    entries: list
    records: list[dict]
    groups: list[_Group]


class ProviderStore:
    def __init__(
        self,
        models_file: Path | None = None,
        sidecar: Path | None = None,
        environ: Mapping[str, str] | None = None,
        registry_env: Callable[[], Mapping[str, str]] | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        run_command: Callable[[str], str] | None = None,
        aws_home: Path | None = None,
    ) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        if models_file is None:
            override = _text(self._environ.get(MODELS_FILE_ENV))
            models_file = Path(override).expanduser() if override else default_settings_path().with_name(MODELS_FILE)
        self.models_file = Path(models_file)
        self.sidecar = Path(sidecar) if sidecar is not None else default_settings_path().with_name("providers.json")
        self._registry_env = registry_env or _registry_environment
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self._run_command = run_command or run_key_command
        self._aws = Resolver(
            self._variable_or_none,
            (lambda command: self._run_command(command)) if run_command else (lambda command: run_key_command(command, AWS_COMMAND_TIMEOUT, "AWS 凭据命令")),
            (lambda command: self._run_command(command))
            if run_command
            else (lambda command: run_key_command(command, AWS_LOGIN_TIMEOUT, "awsAuthRefresh 命令", need_output=False)),
            home=aws_home,
        )
        self._key_cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------------------------------------ public reads

    def list(self) -> list[dict]:
        with self._lock:
            state = self._load()
            return [_provider_view(g, state.entries) for g in state.groups]

    def env_status(self, names: Sequence[str]) -> dict[str, str]:
        registry: Mapping[str, str] | None = None
        out: dict[str, str] = {}
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            if _lookup(self._environ, name):
                out[name] = "set"
                continue
            if registry is None:
                registry = self._registry()
            out[name] = "pending" if _lookup(registry, name) else "unset"
        return out

    def custom_model_ids(self, provider_id: str) -> list[tuple[str, str]]:
        """[(the model's custom id, model id sent to the API)] for the provider's entries, in file order.

        Empty when the provider is unknown or settings.json cannot be read (``list`` is where that error is reported).
        """
        with self._lock:
            try:
                state = self._load()
            except ProviderError:
                return []
            group = next((g for g in state.groups if g.id == provider_id), None)
            if group is None:
                return []
            members = set(group.indices)
            return [(cid, state.entries[i]["model"].strip()) for i, cid, _legacy in _custom_ids(state.entries) if i in members]

    @staticmethod
    def _entry_of(state: Any, custom_model_id: str, display_name: str = "") -> int | None:
        """The index in ``customModels`` of the entry a custom model id names (current id, the older position-based id,
        ``custom:<model>``, and last the display name when it is unique); None when it is none of ours."""
        ids = _custom_ids(state.entries)
        for index, current, _legacy in ids:
            if current == custom_model_id:
                return index
        for index, _current, legacy in ids:
            if legacy == custom_model_id:
                return index
        if custom_model_id.startswith("custom:"):
            model = custom_model_id[len("custom:") :]
            for index, _current, _legacy in ids:
                if state.entries[index]["model"].strip() == model:
                    return index
        if display_name:
            named = [i for i, _current, _legacy in ids if _display_of(state.entries[i]) == display_name]
            if len(named) == 1:
                return named[0]
        return None

    def provider_of(self, custom_model_id: str, display_name: str = "") -> str | None:
        """The id of the provider that owns a custom model id; None when the model is not one of ours (or the file is unreadable)."""
        with self._lock:
            try:
                state = self._load()
            except ProviderError:
                return None
            index = self._entry_of(state, custom_model_id, display_name)
            if index is None:
                return None
            return {i: g.id for g in state.groups for i in g.indices}.get(index)

    def limits_of(self, custom_model_id: str, display_name: str = "") -> tuple[int | None, int | None]:
        """``(context window, largest answer)`` from the entry; a missing window falls back to an exact documented model id.
        Each is None when neither is known (or the model is not ours)."""
        with self._lock:
            try:
                state = self._load()
            except ProviderError:
                return None, None
            index = self._entry_of(state, custom_model_id, display_name)
            if index is None:
                return None, None
            entry = state.entries[index]
            return _positive_int(entry.get("maxContextLimit")) or known_window(entry["model"]), _positive_int(entry.get("maxOutputTokens"))

    def window_of(self, custom_model_id: str, display_name: str = "") -> int | None:
        """The configured window, or the exact model's documented window; None when neither is known (or it is not ours)."""
        return self.limits_of(custom_model_id, display_name)[0]

    # ------------------------------------------------------------------------------- the models WittyCode runs itself

    def api_models(self) -> list[ApiModelSpec]:
        """The models of the API providers that WittyCode's own agent loop runs (every kind, Bedrock's too), in file order. Empty when
        settings.json cannot be read (``list`` is where that error is reported)."""
        with self._lock:
            try:
                state = self._load()
            except ProviderError:
                return []
            owner = {i: g for g in state.groups for i in g.indices}
            specs: list[ApiModelSpec] = []
            for index, custom_id, _legacy in _custom_ids(state.entries):
                group = owner.get(index)
                entry = state.entries[index]
                if group is None:
                    continue
                reasoning = entry.get("reasoningEffort")
                headers = entry.get("extraHeaders")
                extra = entry.get("extraArgs")
                specs.append(
                    ApiModelSpec(
                        id=f"api:{group.id}:{custom_id}",
                        native=entry["model"].strip(),
                        display=_display_of(entry),
                        provider_id=group.id,
                        provider_name=group.name,
                        kind=entry["provider"],
                        base_url=_text(entry.get("baseUrl")),
                        window=_positive_int(entry.get("maxContextLimit")) or known_window(entry["model"]),
                        max_output=_positive_int(entry.get("maxOutputTokens")),
                        reasoning=reasoning if reasoning in EFFORTS else "",
                        images=not (entry["noImageSupport"] if isinstance(entry.get("noImageSupport"), bool) else entry["provider"] == "generic-chat-completion-api"),
                        bearer=entry.get("authMode") == "bearer",
                        headers=tuple((str(k), str(v)) for k, v in headers.items() if isinstance(v, str)) if isinstance(headers, dict) else (),
                        extra_args=dict(extra) if isinstance(extra, dict) else {},
                        key=_entry_key(entry),
                        bedrock=_bedrock_settings(entry.get("bedrock")),
                        thinking=(_positive_int(entry.get("thinkingMaxTokens")) or DEFAULT_THINKING)
                        if entry.get("enableThinking") is True and entry["provider"] in ("anthropic", BEDROCK)
                        else None,
                    )
                )
            return specs

    def api_model(self, model_id: str) -> ApiModelSpec | None:
        return next((spec for spec in self.api_models() if spec.id == model_id), None)

    def api_stamp(self) -> tuple[int, int]:
        """Changes whenever the files the models come from do (their modification times): a cache of ``api_models`` is good while it holds."""

        def stamp(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        return stamp(self.models_file), stamp(self.sidecar)

    def api_key(self, spec: ApiModelSpec) -> str | None:
        """The key for a request to this model, read now: from the environment or the user's registry variables (so one set after
        WittyCode started works at once, there being no process that needs a restart), or the key command's output (kept for
        its time to live). ``None``: the provider needs no key. Raises ``ProviderError`` with a sentence for the user."""
        key = spec.key
        if key.mode == "env":
            return self._variable(key.hint[2:-1])
        if key.mode == "command":
            return self._command_key(key)
        if key.mode == "inline":
            return key.secret
        return None

    def bedrock_plan(self, spec: ApiModelSpec) -> BedrockPlan:
        """Where and how a request to a Bedrock model goes, with everything looked up now: the region, the endpoint, the credentials
        (read from the environment, files, a command or the SSO cache; kept until they near their end) and the request metadata.
        Raises ``ProviderError`` with a sentence for the user."""
        raw = spec.bedrock
        if raw is None:
            raise ProviderError("这不是 Bedrock 的模型。")

        def expand(text: str) -> str:
            return _ENV_ANY.sub(lambda m: self._variable(m.group(1)), text)

        sends_tags = spec.kind in (BEDROCK, "anthropic")
        metadata = tuple((expand(k), expand(v)) for k, v in raw.metadata) if sends_tags else ()
        _check_metadata(metadata)
        settings = BedrockSettings(
            method=raw.method,
            region=expand(raw.region),
            profile=expand(raw.profile),
            base_url=expand(raw.base_url),
            auth_refresh=raw.auth_refresh,
            credential_export=raw.credential_export,
            metadata=metadata,
        )
        try:
            region = self._aws.region(settings)
            access = self._aws.access(settings)
        except AwsError as error:
            raise ProviderError(str(error)) from None
        return BedrockPlan(region, settings.base_url, access, dict(metadata))

    def api_headers(self, spec: ApiModelSpec) -> dict[str, str]:
        """The user's own headers with their ``${VAR}`` references filled in."""
        return {name: _ENV_ANY.sub(lambda m: self._variable(m.group(1)), value) for name, value in spec.headers}

    def _variable_or_none(self, name: str) -> str | None:
        return _lookup(self._environ, name) or _lookup(self._registry(), name)

    def _variable(self, name: str) -> str:
        value = _lookup(self._environ, name) or _lookup(self._registry(), name)
        if value:
            return value
        raise ProviderError(f"环境变量 {name} 没有设置：设置它之后再发送（不需要重启 WittyCode）。")

    def _command_key(self, key: _Key) -> str:
        ttl = (key.ttl or 300_000) / 1000
        cached = self._key_cache.get(key.hint)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        secret = self._run_command(key.hint)
        self._key_cache[key.hint] = (now + ttl, secret)
        return secret

    # ----------------------------------------------------------------------------------------------- public writes

    def save(self, draft: dict) -> dict:
        spec = _parse_draft(draft)
        with self._lock:
            state = self._load(writing=True)
            target = next((g for g in state.groups if g.id == spec.id), None)
            for group in state.groups:
                if group is not target and group.key == spec.key:
                    raise ProviderError(f"已经有一个 Provider 用了同样的接口类型和地址：「{group.name}」，请直接编辑它。")
            members = [(i, state.entries[i]) for i in target.indices] if target else []

            inline_secret = None
            if spec.key_mode == "inline":
                stored = _group_key_info([m for _, m in members])
                if stored.mode != "inline":
                    raise ProviderError("这个 Provider 的配置里已经没有明文密钥了，请改用环境变量。")
                if spec.key_hint not in ("", stored.hint):
                    raise ProviderError("密钥不能直接写进配置：请改用环境变量。")
                inline_secret = stored.secret
            first_headers = members[0][1].get("extraHeaders") if members else None
            headers = _resolve_headers(spec.headers, first_headers if isinstance(first_headers, dict) else {})

            pool = list(members)
            updated: dict[int, dict] = {}
            added: list[dict] = []
            for row in spec.models:
                match = next((p for p in pool if p[1]["model"].strip() == row.id), None)
                if match is not None:
                    pool.remove(match)
                built = _build_entry(match[1] if match else None, spec, row, headers, inline_secret)
                if match is None:
                    added.append(built)
                else:
                    updated[match[0]] = built

            if target is None:
                entries = [*state.entries, *added]
            else:
                last = target.indices[-1]
                entries = []
                for index, existing in enumerate(state.entries):
                    if index in updated:
                        entries.append(updated[index])
                    elif index not in target.indices:
                        entries.append(existing)
                    if index == last:
                        entries.extend(added)

            record: dict[str, Any] = {"id": spec.id, "name": spec.name, "kind": spec.kind, "baseUrl": spec.base_url}
            if spec.kind == BEDROCK:
                record["region"] = spec.key[1]
            records: list[dict] = []
            placed = False
            for old in state.records:
                if old["id"] == spec.id:
                    if not placed:
                        records.append(record)
                        placed = True
                elif _record_key(old) != spec.key:
                    records.append(old)
            if not placed:
                records.append(record)
            live = {g.key for g in _build_groups(entries, records)}
            records = [r for r in records if _record_key(r) in live]

            if entries != state.entries:
                state.doc["customModels"] = entries
                self._write_settings(state.doc)
            if records != state.records:
                self._write_sidecar(records)
            saved = next((g for g in _build_groups(entries, records) if g.id == spec.id), None)
            if saved is None:
                raise ProviderError("保存后在 settings.json 里找不到这个 Provider 的模型，请检查模型 ID 和地址。")
            return _provider_view(saved, entries)

    def remove(self, provider_id: str) -> None:
        with self._lock:
            state = self._load(writing=True)
            target = next((g for g in state.groups if g.id == provider_id), None)
            if target is not None:
                gone = set(target.indices)
                state.doc["customModels"] = [e for i, e in enumerate(state.entries) if i not in gone]
                self._write_settings(state.doc)
            records = [r for r in state.records if r["id"] != provider_id]
            if records != state.records:
                self._write_sidecar(records)

    # -------------------------------------------------------------------------------------- connection test / models

    async def test(self, draft: dict) -> dict:
        return await asyncio.to_thread(self._probe, draft)

    async def list_models(self, draft: dict) -> list[str]:
        result = await asyncio.to_thread(self._probe, draft)
        return list(result.get("models") or [])

    async def model_details(self, draft: dict) -> list[dict[str, Any]]:
        """The server's model ids and figures, with exact documented windows as a fallback and their source marked."""
        result = await asyncio.to_thread(self._probe, draft)
        figures = {entry["id"]: entry for entry in result.get("details") or ()}
        models = []
        for model_id in result.get("models") or ():
            entry = dict(figures.get(model_id, {"id": model_id}))
            if entry.get("contextWindow"):
                entry["contextSource"] = "server"
            elif window := known_window(model_id):
                entry.update(contextWindow=window, contextSource="catalog")
            models.append(entry)
        return models

    def _probe(self, draft: Any) -> dict[str, Any]:
        try:
            return self._probe_unchecked(draft)
        except ProviderError as error:
            return _fail(str(error))
        except Exception as error:  # a test must never take the page down
            log.warning("provider test failed unexpectedly: %s", type(error).__name__)
            return _fail(f"测试时出了意外：{type(error).__name__}")

    def _probe_unchecked(self, draft: Any) -> dict[str, Any]:
        spec = _parse_draft(draft, connection_only=True)
        if spec.kind == BEDROCK:
            return {"ok": False, "text": BEDROCK_TEST_NOTE, "ms": None, "models": None}
        key = self._credential(spec)
        stored_headers: Mapping[str, Any] = {}
        with self._lock:
            try:
                state = self._load()
            except ProviderError:
                state = None
            if state is not None:
                group = next((g for g in state.groups if g.id == spec.id), None)
                raw = state.entries[group.indices[0]].get("extraHeaders") if group else None
                stored_headers = raw if isinstance(raw, dict) else {}
        headers = {"Accept": "application/json", "User-Agent": "WittyCode"}
        if spec.kind == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["Authorization" if spec.bearer else "x-api-key"] = f"Bearer {key}" if spec.bearer else key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        for name, value in _resolve_headers(spec.headers, stored_headers).items():
            for existing in [k for k in headers if k.lower() == name.lower()]:
                del headers[existing]
            headers[name] = self._expand(value, name)

        host = _host(spec.base_url)
        request = urllib.request.Request(_models_url(spec), headers=headers, method="GET")
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read(MAX_BODY_BYTES)
        except urllib.error.HTTPError as error:
            code, reason = error.code, str(error.reason or "")
            try:
                error.close()
            except Exception:  # an HTTPError built without a body has nothing to close
                pass
            return _fail(_http_problem(code, reason))
        except (urllib.error.URLError, OSError) as error:
            return _fail(_network_problem(host, error))
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
            ids = _model_ids(payload)
            details = _model_details(payload)
        except ValueError:
            ids, details = None, []
        text = f"连接成功 · {elapsed} ms" + (f" · {len(ids)} 个模型" if ids is not None else "")
        return {"ok": True, "text": text, "ms": elapsed, "models": ids, "details": details}

    def _credential(self, spec: _Draft) -> str | None:
        if spec.key_mode == "env":
            return self._env_value(spec.key_hint[2:-1], "请求没有带密钥")
        if spec.key_mode == "command":
            return self._run_command(spec.key_hint)
        if spec.key_mode == "inline":
            with self._lock:
                state = self._load()
                group = next((g for g in state.groups if g.id == spec.id), None)
                info = _group_key_info([state.entries[i] for i in group.indices]) if group else _NO_KEY
            if info.secret is None:
                raise ProviderError("配置里没有找到保存的密钥。")
            return info.secret
        return None

    def _env_value(self, name: str, consequence: str) -> str:
        value = _lookup(self._environ, name)
        if value:
            return value
        if _lookup(self._registry(), name):
            raise ProviderError(f"环境变量 {name} 是在 WittyCode 启动之后设置的：重启 WittyCode 再试")
        raise ProviderError(f"环境变量 {name} 没有设置，{consequence}")

    def _expand(self, value: str, header: str) -> str:
        return _ENV_ANY.sub(lambda m: self._env_value(m.group(1), f"请求头 {header} 要用它"), value)

    def _registry(self) -> Mapping[str, str]:
        try:
            return self._registry_env() or {}
        except Exception:
            return {}

    # ------------------------------------------------------------------------------------------------- file access

    def _load(self, *, writing: bool = False) -> _State:
        doc = self._read_settings()
        raw = doc.get("customModels")
        if raw is not None and not isinstance(raw, list):
            if writing:
                raise ProviderError("settings.json 里的 customModels 不是数组，WittyCode 没有改动它。")
            raw = []
        entries = raw if isinstance(raw, list) else []
        records = self._read_sidecar()
        return _State(doc, entries, records, _build_groups(entries, records))

    def _read_settings(self) -> dict:
        try:
            data = self.models_file.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError:
            raise ProviderError("读不了 settings.json，WittyCode 没有改动它。") from None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ProviderError("settings.json 不是 UTF-8 文本，WittyCode 没有改动它。") from None
        if not text.strip():
            return {}
        try:
            doc = json.loads(text)
        except ValueError:
            raise ProviderError("settings.json 不是合法的 JSON，WittyCode 没有改动它：请先修好这个文件。") from None
        if not isinstance(doc, dict):
            raise ProviderError("settings.json 的最外层不是对象，WittyCode 没有改动它。")
        return doc

    def _read_sidecar(self) -> list[dict]:
        try:
            data = json.loads(self.sidecar.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        records = data.get("providers") if isinstance(data, dict) else None
        return [r for r in records if _valid_record(r)] if isinstance(records, list) else []

    def _write_settings(self, doc: dict) -> None:
        path = self.models_file
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            if path.exists() and not backup.exists():
                shutil.copy2(path, backup)
        except OSError:
            raise ProviderError("没法备份 settings.json，所以没有改动它。") from None
        self._atomic_write(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", "写不了 settings.json，可能被别的程序占用了。")

    def _write_sidecar(self, records: list[dict]) -> None:
        payload = json.dumps({"version": 1, "providers": records}, indent=2, ensure_ascii=False) + "\n"
        try:
            self._atomic_write(self.sidecar, payload, "")
        except ProviderError:
            log.warning("could not save the provider names to %s", self.sidecar)

    @staticmethod
    def _atomic_write(path: Path, payload: str, failure: str) -> None:
        temporary = path.with_name(path.name + ".wittycode-tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            for attempt in range(5):
                try:
                    os.replace(temporary, path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProviderError(failure or "写文件失败。") from None
