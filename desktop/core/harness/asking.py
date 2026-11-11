"""The ``ask_user`` tool of the application's own harness: the model asks the user a few short questions while it works.

Droid has its AskUser tool and Codex its request-for-input; this engine had no such tool, so its model could only write the
question as text and end the turn, and in practice it decided on its own. The tool shows the page's question card (the same
one the other engines use), waits for the answers and gives them back as the tool's result. A question skipped is a result
too: the model is told not to ask again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from desktop.core.prompts import QuestionView

NAME = "ask_user"
MAX_QUESTIONS = 4
MAX_OPTIONS = 6
HEADER_LIMIT = 30

DECLARATION: dict[str, Any] = {
    "name": NAME,
    "description": (
        "Ask the user one to four short questions and wait for the answers, then continue the task. Use it when a decision is "
        "the user's to make and the project cannot answer it: which of several reasonable approaches to take, a requirement "
        "that is ambiguous in a way that changes what you would build, a risky or irreversible step. Do not use it for what "
        "you can find out by reading the project or running a command, and do not ask the same thing twice. Give each "
        "question 2 to 4 concrete options, the recommended one first (the user can also type another answer)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": "The questions, in the order they should be asked.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question, one sentence."},
                        "header": {"type": "string", "description": "A topic of one to three words, shown above the question."},
                        "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": MAX_OPTIONS, "description": "The answers to choose from."},
                        "multi_select": {"type": "boolean", "description": "True when more than one option may be chosen."},
                    },
                    "required": ["question", "options"],
                },
            }
        },
        "required": ["questions"],
    },
}

SKIPPED = (
    "The user skipped the question without answering. Do not ask it again: go on with your best judgement and say what you "
    "assumed, or explain what you need."
)
USAGE = "ask_user needs `questions`: a list of one to four objects {question, header, options, multi_select}."


def _option(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("label") or value.get("text") or value.get("value")
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def parse(args: Mapping[str, Any]) -> tuple[tuple[QuestionView, ...], str]:
    """The questions of a call, or (empty) why they cannot be asked: the sentence goes to the model as the tool's result."""
    raw = args.get("questions")
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        return (), USAGE
    views: list[QuestionView] = []
    for number, item in enumerate(raw[:MAX_QUESTIONS], 1):
        if not isinstance(item, Mapping):
            return (), f"Question {number} is not an object. {USAGE}"
        text = " ".join(str(item.get("question") or "").split())
        if not text:
            return (), f"Question {number} has no text. {USAGE}"
        options: list[str] = []
        for option in item.get("options") or ():
            label = _option(option)
            if label and label not in options:
                options.append(label)
        header = " ".join(str(item.get("header") or "").split())[:HEADER_LIMIT]
        views.append(QuestionView(index=len(views), topic=header, question=text, options=tuple(options[:MAX_OPTIONS]), multi_select=bool(item.get("multi_select"))))
    return tuple(views), ""


def questionnaire(args: Mapping[str, Any]) -> str:
    """The questions in the form Droid's AskUser tool carries them, which the page already reads for the row's summary."""
    views, _ = parse(args)
    lines: list[str] = []
    for number, view in enumerate(views, 1):
        lines.append(f"{number}. [question] {view.question}")
        if view.topic:
            lines.append(f"[topic] {view.topic}")
        lines += [f"[option] {option}" for option in view.options]
    return "\n".join(lines)


def answer_text(answer: str | Sequence[str]) -> str:
    return answer if isinstance(answer, str) else ", ".join(answer)


def result(views: Sequence[QuestionView], answers: Sequence[str | Sequence[str]]) -> str:
    """What the model reads: each question with the answer the user gave (their own words when they typed one)."""
    lines = ["The user answered:"]
    for number, (view, answer) in enumerate(zip(views, answers), 1):
        lines.append(f"{number}. {view.question}")
        lines.append(f"   Answer: {answer_text(answer).strip() or '(no answer)'}")
    return "\n".join(lines)
