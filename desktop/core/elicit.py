"""An MCP-style elicitation: an engine (Devin's ``elicitation/create``) or an MCP server asking the user for something.

The request (app-server schema, MCP 2025-11-25) carries ``serverName``, a ``message`` for the user and one of:

* ``mode: "form"`` with a ``requestedSchema``: a flat object whose properties are the fields to fill in (a string, a
  number / integer, a boolean, a single choice, a multiple choice), each with a title, a description, a default;
* ``mode: "url"`` with a ``url`` (and an ``elicitationId``): something the user has to do in the browser (a sign-in);
* ``mode: "openai/form"`` / ``"openaiForm"``: OpenAI's own form modes, the schema free-form (read here when it looks
  like the first kind).

The reply is ``{action: "accept" | "decline" | "cancel", content}``: ``content`` holds a value for each field when the
user accepted. A request with no fields is a plain yes / no (an approval is asked this way too, as far as is known).

The questions are shown with the application's own question cards (one question per field); this module turns the
request into them and the answers back into ``content``. Pure functions, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from desktop.core.prompts import QuestionView

ALLOW, DENY = "允许", "拒绝"
SESSION, ALWAYS = "允许，本会话内同一工具不再询问", "允许，以后一律允许这个工具"
OPEN, YES, NO = "打开链接并继续", "是", "否"
FORM_MODES = ("form", "openai/form", "openaiForm")
MAX_FIELDS = 12
APPROVAL = "mcp_tool_call"
"""``_meta.codex_approval_kind`` of a request made before a tool of an MCP server runs (the key one app-server
implementation uses; ``persist`` lists how long the user may allow it for)."""
DETAIL_CHARS = 600


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    kind: str
    """``string`` | ``number`` | ``integer`` | ``boolean`` | ``choice`` | ``choices``."""
    required: bool
    labels: tuple[str, ...] = ()
    values: tuple[Any, ...] = ()
    """For a choice: the value each label stands for."""


@dataclass(frozen=True, slots=True)
class Plan:
    mode: str
    """``form`` | ``confirm`` | ``url`` | ``approval`` (may this tool of an MCP server run)."""
    server: str
    views: tuple[QuestionView, ...]
    fields: tuple[Field, ...] = ()
    url: str = ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _same_text(left: str, right: str) -> bool:
    """Only identical wording is redundant; collapse whitespace without joining words, never guess at translations."""
    return " ".join(left.split()) == " ".join(right.split())


def _choice(spec: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[Any, ...]] | None:
    """Labels and values of a single-choice schema (``enum`` with or without ``enumNames``, or ``oneOf`` of ``const``s)."""
    if isinstance(spec.get("oneOf"), list):
        options = [(_text(o.get("title")) or str(o.get("const")), o.get("const")) for o in spec["oneOf"] if isinstance(o, Mapping) and "const" in o]
        return (tuple(l for l, _ in options), tuple(v for _, v in options)) if options else None
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        names = spec.get("enumNames") if isinstance(spec.get("enumNames"), list) else []
        values = tuple(spec["enum"])
        return tuple(str(names[i]) if i < len(names) else str(v) for i, v in enumerate(values)), values
    return None


def _field(key: str, spec: Any, required: bool) -> Field | None:
    if not isinstance(spec, Mapping):
        return None
    kind = spec.get("type")
    choice = _choice(spec)
    if kind == "array":
        items = spec.get("items")
        many = _choice(items) if isinstance(items, Mapping) else None
        if many is None and isinstance(items, Mapping) and isinstance(items.get("anyOf"), list):
            options = [(_text(o.get("title")) or str(o.get("const")), o.get("const")) for o in items["anyOf"] if isinstance(o, Mapping) and "const" in o]
            many = (tuple(l for l, _ in options), tuple(v for _, v in options)) if options else None
        return Field(key, "choices", required, *many) if many else None
    if choice is not None and kind in (None, "string"):
        return Field(key, "choice", required, *choice)
    if kind in ("string", "number", "integer", "boolean"):
        return Field(key, kind, required)
    return None


def plan_of(params: Mapping[str, Any]) -> Plan | None:
    """What to ask the user for this request (``None``: it is a kind of request this application cannot show)."""
    server = _text(params.get("serverName")) or "MCP"
    message = _text(params.get("message")).strip()
    mode = _text(params.get("mode"))
    if mode == "url":
        url = _text(params.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            return None
        view = QuestionView(0, server, f"{message}\n{url}".strip(), (OPEN, DENY), False)
        return Plan("url", server, (view,), url=url)
    if mode not in FORM_MODES:
        return None
    meta = params.get("_meta") if isinstance(params.get("_meta"), Mapping) else {}
    if meta.get("codex_approval_kind") == APPROVAL:
        return _approval(server, message, meta)
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not properties:
        if schema is not None and not isinstance(schema, Mapping):
            return None
        return Plan("confirm", server, (QuestionView(0, server, message or f"{server} 请求确认", (ALLOW, DENY), False),))
    if not isinstance(properties, Mapping) or len(properties) > MAX_FIELDS:
        return None
    required = set(schema.get("required") or ()) if isinstance(schema.get("required"), list) else set()
    fields: list[Field] = []
    views: list[QuestionView] = []
    for index, (key, spec) in enumerate(properties.items()):
        field = _field(str(key), spec, str(key) in required)
        if field is None:
            return None
        title = _text(spec.get("title")).strip() or str(key)
        description = _text(spec.get("description")).strip()
        if _same_text(title, description):
            description = ""
        question = title + (f"（{description}）" if description else "")
        if index == 0 and message:
            if _same_text(message, description):
                question = message
            elif not _same_text(message, title):
                question = f"{message}\n{question}"
        topic = "" if _same_text(title, question) else title
        options = {"boolean": (YES, NO), "choice": field.labels, "choices": field.labels}.get(field.kind, ())
        fields.append(field)
        views.append(QuestionView(index, topic, question, tuple(options), field.kind == "choices"))
    return Plan("form", server, tuple(views), tuple(fields))


def _approval(server: str, message: str, meta: Mapping[str, Any]) -> Plan:
    """Codex asking whether a tool of an MCP server may run: the question says which tool, what it is for and with what."""
    lines = [message or f"允许 {server} 运行一个工具吗？"]
    if _text(meta.get("tool_description")).strip():
        lines.append(_text(meta["tool_description"]).strip())
    shown = meta.get("tool_params_display")
    params = meta.get("tool_params")
    if isinstance(shown, list) and shown:
        detail = "；".join(f"{_text(p.get('display_name')) or _text(p.get('name'))}={p.get('value')}" for p in shown if isinstance(p, Mapping))
    elif isinstance(params, Mapping) and params:
        detail = "；".join(f"{key}={value}" for key, value in params.items())
    else:
        detail = ""
    if detail:
        lines.append("参数：" + (detail if len(detail) <= DETAIL_CHARS else detail[: DETAIL_CHARS - 1] + "…"))
    persist = meta.get("persist") if isinstance(meta.get("persist"), list) else []
    options = (ALLOW, *([SESSION] if "session" in persist else []), *([ALWAYS] if "always" in persist else []), DENY)
    return Plan("approval", server, (QuestionView(0, server, "\n".join(lines), options, False),))


def _convert(field: Field, answer: Sequence[str]) -> Any:
    """The value of one field from the answer(s) the user gave; ``None``: left empty. ``ValueError``: not acceptable."""
    texts = [a.strip() for a in answer if a and a.strip()]
    if field.kind == "choices":
        return [field.values[field.labels.index(t)] if t in field.labels else t for t in texts] or None
    text = texts[0] if texts else ""
    if not text:
        return None
    if field.kind == "boolean":
        if text in (YES, "true", "True", "yes"):
            return True
        if text in (NO, "false", "False", "no"):
            return False
        raise ValueError(text)
    if field.kind == "integer":
        return int(text)
    if field.kind == "number":
        number = float(text)
        return int(number) if number.is_integer() and "." not in text else number
    if field.kind == "choice":
        return field.values[field.labels.index(text)] if text in field.labels else text
    return text


def reply_for(plan: Plan, answers: Sequence[Sequence[str]] | None) -> dict[str, Any]:
    """The reply to Codex for the user's answers (``None``: the user skipped the question)."""
    if answers is None:
        return {"action": "cancel", "content": None, "_meta": None}
    first = answers[0][0] if answers and answers[0] else ""
    if plan.mode == "approval":
        if first not in (ALLOW, SESSION, ALWAYS):
            return {"action": "decline", "content": None, "_meta": None}
        persist = {SESSION: "session", ALWAYS: "always"}.get(first)
        return {"action": "accept", "content": None, "_meta": {"persist": persist} if persist else None}
    if plan.mode in ("confirm", "url"):
        agreed = first in (ALLOW, OPEN)
        return {"action": "accept" if agreed else "decline", "content": {} if agreed and plan.mode == "confirm" else None, "_meta": None}
    content: dict[str, Any] = {}
    try:
        for field, answer in zip(plan.fields, answers):
            value = _convert(field, answer)
            if value is None:
                if field.required:
                    raise ValueError(field.key)
                continue
            content[field.key] = value
    except ValueError:
        return {"action": "decline", "content": None, "_meta": None}
    return {"action": "accept", "content": content, "_meta": None}
