"""A goal the user gave a conversation (``/goal <text>``): the model works toward it turn after turn, on its own, on any engine.

The goal is carried by the messages, so nothing here depends on how an engine runs its turns. The first message is the goal
with a note on how to end it; after each turn ``step`` reads the last line of the model's last message (``GOAL: DONE`` when
the goal is really met, ``GOAL: BLOCKED …`` when it cannot go on without the user) and decides: another turn, whose message
puts the goal in front of the model again and asks it to check the result against it (the model is the judge, in the
conversation that holds all the work), or the end of the goal with a sentence for the user. Three things end it without the
model's say: a round limit, two rounds in a row in which the model touched no tool and said nothing of being done (it is
talking, not working), and the user (a stop, a failed turn: ``AppCore``). Nothing asks the model anything outside the
conversation, so a goal costs no request beyond the turns themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUNDS = 20
"""Turns a goal may take at most."""
IDLE = 2
"""Rounds in a row without a tool call and without a mark that end the goal."""

GOAL_DONE, GOAL_BLOCKED = "GOAL: DONE", "GOAL: BLOCKED"
"""The last line of a message that ends a goal."""

STOP_WORDS = frozenset({"off", "stop", "clear", "cancel", "关闭", "停止", "取消", "清除"})
"""``/goal`` followed by one of these ends the goal in force instead of setting a new one."""

_MARK = re.compile(r"^[\s>*_`#-]*GOAL:\s*(DONE|BLOCKED)\b(.*)$", re.IGNORECASE)

_HOW_TO_END = f"达成了：最后一行单独写 {GOAL_DONE}。没达成：说明还差什么，接着做。没有我就做不下去（要我拍板、缺权限、坏了修不好）：最后一行单独写 {GOAL_BLOCKED}，再写原因。"


def opening(text: str) -> str:
    """The first message of a goal: the goal, then how the model ends it."""
    return f"{text.strip()}\n\n（这是一个目标，请一直做到完成。做完后先对照目标检查结果，再按下面说的结束。{_HOW_TO_END}）"


@dataclass(slots=True)
class Goal:
    text: str
    rounds: int = 0
    """Turns taken toward it."""
    idle: int = 0
    """Turns in a row that touched no tool and did not end it."""

    def next_message(self) -> str:
        """The message of the next turn: the goal again, to be checked against the result so far."""
        return f"继续推进目标（第 {self.rounds + 1}/{ROUNDS} 轮）：{self.text}\n对照目标检查现在的结果（需要就运行测试、读输出）。{_HOW_TO_END}"


@dataclass(frozen=True, slots=True)
class Step:
    prompt: str = ""
    """The message that starts the next turn; empty: the goal is over."""
    notice: str = ""
    """What the user is told (the end of the goal, and why)."""


def mark_of(answer: str) -> tuple[str, str]:
    """``("done" | "blocked" | "", the rest of the line)`` for the last line of ``answer``. Only the last line counts: a
    message that merely mentions the mark (quoting its instructions) does not end the goal."""
    lines = [line for line in answer.splitlines() if line.strip()]
    found = _MARK.match(lines[-1]) if lines else None
    if found is None:
        return "", ""
    return found.group(1).lower(), found.group(2).strip(" \t*_`.。:：-")


def step(goal: Goal, answer: str, worked: bool) -> Step:
    """What follows a turn of the goal. ``answer``: the model's last message of the turn; ``worked``: it called a tool."""
    goal.rounds += 1
    mark, rest = mark_of(answer)
    if mark == "done":
        return Step(notice=f"目标完成了（用了 {goal.rounds} 轮）。")
    if mark == "blocked":
        return Step(notice="目标先停下了，模型需要你：" + (rest or "见上一条回复。"))
    goal.idle = 0 if worked else goal.idle + 1
    if goal.idle >= IDLE:
        return Step(notice=f"模型连续 {IDLE} 轮没有动手，也没有说目标完成，目标先停在这里。可以回复它，或重新发 /goal。")
    if goal.rounds >= ROUNDS:
        return Step(notice=f"已经做了 {ROUNDS} 轮，目标还没有完成，先停在这里。看看进展；需要的话再发一次 /goal。")
    return Step(goal.next_message())
