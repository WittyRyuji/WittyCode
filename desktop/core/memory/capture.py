"""Notes the user asks for in so many words: "记住：提交信息用中文", "从现在起回答都用中文", "remember that the tests need the db".

This needs no model: the application sees the message before it is sent (``MemoryLaunch.prompt_hook``) and, when it says
plainly that something is to be kept, writes the note itself. The patterns are narrow on purpose. A wrong note is
visible and can be forgotten, but a memory full of things the user only said in passing is worse than a missing one, so
a plain "以后再说", a question, or a bare "always" in a sentence about code is not a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_NOTE = 400
MIN_BODY = 4
TITLE_CHARS = 30

# "记住 …": the user says it is to be remembered; what follows is the note.
_ASKED = re.compile(
    r"(?:请|麻烦|务必|一定|你要|你得|你需要)?(?:帮我|给我)?"
    r"(?:记住|记一下|记下来|记下|记着|切记|别忘了|不要忘了|不要忘记|勿忘|要记得|请记得|你记得要)"
    r"|(?:please\s+)?remember\s+(?:that|this|to)\b|\bplease\s+remember\b|\bdon'?t\s+forget\b|\bkeep\s+in\s+mind\b"
    r"|\bnote\s+to\s+self\b|\bmake\s+a\s+note\s+(?:that|of)\b",
    re.IGNORECASE,
)

# "以后 / 每次 / 永远 …" followed by something to do or not to do: a standing instruction. The sentence is the note.
_STANDING_ZH = re.compile(
    r"(?:^|[\s，,：:])(?:"
    # (after these, "都" is part of the instruction: 以后都用中文)
    r"(?:以后(?!再)|今后|往后|从今往后|从现在(?:开始|起)|从此)[^，,。！!？?；;\n]{0,3}?(?:都|一律|必须|务必|不要|别|不能|不许|禁止|先|只|总是|默认|要|请|用|使用)"
    # (after these it is not: 每次都报错 only says what happens)
    r"|(?:永远|每次|任何时候|无论何时)[^，,。！!？?；;\n]{0,3}?(?:一律|必须|务必|不要|别|不能|不许|禁止|先|只|总是|默认|要|请|用|使用)"
    r")"
)
_STANDING_EN = re.compile(
    r"\b(?:from\s+now\s+on|going\s+forward|in\s+the\s+future|every\s+time|each\s+time)\b[^.!?\n]{0,60}?"
    r"\b(?:always|never|must|should|use|do|don'?t|do\s+not|make\s+sure|please|reply|answer|write|run|commit|keep|prefer)\b"
    r"|^\s*(?:please\s+)?(?:always|never)\s+(?!mind\b)\w+",
    re.IGNORECASE,
)

_HEDGE = re.compile(r"可能|也许|大概|或许|估计|说不定|会不会|应该会|maybe|perhaps|might|probably", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")
_QUESTION = re.compile(r"[?？]\s*$|(?:吗|么|呢)\s*[?？]?\s*$")
_LEAD = re.compile(r"^[\s，,：:、;；\-—>]+|^(?:一下|下来|着|的是|：|:)+")
_TAIL = re.compile(r"[\s,，、]*(?:吧|哦|哈|啊|呀|好吗|行吗|谢谢|thanks?|please)?[\s。.!！]*$", re.IGNORECASE)
_CLAUSE_END = re.compile(r"[，,。；;：:！!？?\n]")

_GLOBAL = re.compile(
    r"(?:所有|任何|每个|全部)(?:项目|仓库|工程)|全局|无论(?:什么|哪个)项目|不管(?:什么|哪个)项目|称呼我|叫我|我叫|我的名字|"
    r"我(?:喜欢|习惯|偏好|希望)|用中文|说中文|中文回答|回答.{0,4}中文|"
    r"\b(?:every|all)\s+projects?\b|\beverywhere\b|\bcall\s+me\b|\bmy\s+name\s+is\b|\bI\s+(?:prefer|like)\b",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(r"喜欢|习惯|偏好|希望你|称呼|语气|风格|\bprefer\b|\bI\s+like\b|\bstyle\b", re.IGNORECASE)
_RULE = re.compile(r"必须|不要|别|不能|不许|禁止|一律|务必|总是|每次|默认|用(?!于)|使用|\buse\b|\bnever\b|\balways\b|\bmust\b|\bdon'?t\b|\bdo\s+not\b|\bshould\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Capture:
    title: str
    text: str
    kind: str
    scope: str
    """``global`` | ``project`` (``project`` only when the caller has a project)."""
    tags: tuple[str, ...] = ("自动记下",)


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_END.split(text) if part.strip()]


def _clean(sentence: str) -> str:
    body = _LEAD.sub("", sentence.strip())
    body = _TAIL.sub("", body).strip()
    return body.strip(" \t\"'“”「」")


def _title(body: str) -> str:
    first = _CLAUSE_END.split(body, maxsplit=1)[0].strip()
    if len(first) < 6:
        first = body.strip()
    if len(first) <= TITLE_CHARS:
        return first.rstrip("，,、 ")
    cut = first[:TITLE_CHARS]
    space = cut.rfind(" ")
    if space >= 12:
        cut = cut[:space]
    return cut.rstrip("，,、 ") + "…"


def _cap(body: str) -> str:
    if len(body) <= MAX_NOTE:
        return body
    cut = body[:MAX_NOTE]
    for mark in ("。", "；", ". ", "\n"):
        at = cut.rfind(mark)
        if at >= MAX_NOTE * 0.5:
            return cut[: at + 1].strip()
    return cut.strip()


def detect(text: str, *, has_project: bool = True) -> Capture | None:
    """The note the message asks for, or ``None`` when it asks for none."""
    sentences = _sentences(text)
    for index, sentence in enumerate(sentences):
        if _QUESTION.search(sentence):
            continue
        asked = _ASKED.search(sentence)
        body = ""
        if asked is not None:
            body = _clean(sentence[asked.end() :])
            if len(body) < MIN_BODY:
                body = _clean(sentence[: asked.start()])
            if len(body) < MIN_BODY and index + 1 < len(sentences):
                body = _clean(sentences[index + 1])
        elif not _HEDGE.search(sentence) and (_STANDING_ZH.search(sentence) or _STANDING_EN.search(sentence)):
            body = _clean(sentence)
        if len(body) < MIN_BODY:
            continue
        body = _cap(body)
        scope = "global" if _GLOBAL.search(body) or not has_project else "project"
        kind = "preference" if _PREFERENCE.search(body) else "convention" if _RULE.search(body) else "fact"
        return Capture(_title(body), body, kind, scope)
    return None
