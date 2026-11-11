"""A message that says plainly "remember this" is written down by the application itself (``core/memory/capture``)."""

from __future__ import annotations

import pytest

from desktop.core.hooks import PromptEvent
from desktop.core.memory.capture import MAX_NOTE, detect
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import MemoryStore

ASKS = [
    ("请记住：提交信息用中文", "提交信息用中文"),
    ("记住这个项目的 e2e 要整文件连跑 20 次才算过", "这个项目的 e2e 要整文件连跑 20 次才算过"),
    ("记一下：部署命令是 make ship-it", "部署命令是 make ship-it"),
    ("不要忘了提交前跑 lint", "提交前跑 lint"),
    ("我喜欢简短的回答，记住", "我喜欢简短的回答"),
    ("以后回答都用中文", "以后回答都用中文"),
    ("从现在开始不要再用 emoji", "从现在开始不要再用 emoji"),
    ("每次改完都要跑 pytest", "每次改完都要跑 pytest"),
    ("永远不要在 main 上直接提交", "永远不要在 main 上直接提交"),
    ("今后请先写测试再写代码。谢谢", "今后请先写测试再写代码"),
    ("Remember that the staging db is reset nightly.", "the staging db is reset nightly"),
    ("from now on use pnpm instead of npm", "from now on use pnpm instead of npm"),
    ("Please always reply in Chinese", "Please always reply in Chinese"),
    ("always use tabs", "always use tabs"),
]
NOT_ASKS = [
    "以后再说吧",
    "你还记住了吗？",
    "记得保存文件",
    "这个函数总是返回 None，帮我看看",
    "我们每次运行都报错，怎么办？",
    "每次都报错，帮我看看",
    "永远是同一个错误",
    "一直都是这样的吗",
    "never mind, I fixed it",
    "把这个改成 async，以后可能还要加缓存",
    "Does this function always return a list?",
    "",
    "   ",
    "记住",
]


@pytest.mark.parametrize(("message", "body"), ASKS)
def test_a_message_that_asks_to_remember_gives_the_note(message, body):
    found = detect(message)
    assert found is not None and found.text == body, message
    assert found.title and len(found.title) <= 31 and found.tags == ("自动记下",)


@pytest.mark.parametrize("message", NOT_ASKS)
def test_a_message_that_only_talks_asks_for_nothing(message):
    assert detect(message) is None, message


def test_the_kind_follows_the_words_and_the_scope_follows_who_it_is_about():
    assert detect("我喜欢简短的回答，记住").kind == "preference"
    assert detect("记住不要提交 .env").kind == "convention"
    assert detect("记住部署命令是 make ship-it").kind == "fact"
    assert detect("请记住：回答都用中文").scope == "global", "about how the user likes to be answered: everywhere"
    assert detect("记住这个项目的端口是 8080").scope == "project"
    assert detect("记住这个项目的端口是 8080", has_project=False).scope == "global", "without a project there is only the global memory"


def test_a_long_message_gives_a_short_note_and_a_title_that_is_not_the_whole_note():
    long = "记住：" + "这是一条很长的说明，" * 120
    found = detect(long)
    assert found is not None and len(found.text) <= MAX_NOTE
    assert len(found.title) <= 31


def test_only_the_first_request_in_a_message_is_taken():
    found = detect("记住：测试用 pytest。另外记住：构建用 make。")
    assert found is not None and found.text == "测试用 pytest"


def test_a_hedge_makes_a_standing_instruction_a_guess():
    assert detect("以后可能要用 pnpm") is None
    assert detect("以后要用 pnpm") is not None


# ----------------------------------------------------------------------------- the hook


def event(text, project, **more):
    return PromptEvent("codex", project, text, **more)


def test_the_hook_writes_the_note_and_says_so(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    project = tmp_path / "proj"
    project.mkdir()
    context = launch.prompt_hook(event("记住：部署命令是 make ship-it", project))
    assert context is not None and context.text == "" and context.ids == frozenset()
    assert context.notice.startswith("已记下「部署命令是 make ship-it」") and "共享记忆" in context.notice
    (entry,) = MemoryStore(launch.root).entries()
    assert (entry.title, entry.text, entry.scope, entry.kind, entry.tags) == ("部署命令是 make ship-it", "部署命令是 make ship-it", "project", "fact", ("自动记下",))
    assert entry.source == "user" and entry.project == str(project.resolve()), "the user said it: it is theirs, in their project"


def test_the_note_just_written_is_not_also_offered_back_as_a_related_note(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    context = launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path))
    assert "带上了" not in context.notice
    later = launch.prompt_hook(event("部署命令是什么，怎么 make ship-it", tmp_path, first=True))
    assert later is not None and "带上了 1 条相关记忆" in later.notice and "已记下" not in later.notice
    assert launch.prompt_hook(event("部署命令是什么，怎么 make ship-it", tmp_path)) is None, "only a conversation's first message brings notes"


def test_the_same_request_twice_makes_one_note(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    assert launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path)).notice.startswith("已记下")
    again = launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path, seen=frozenset()))
    assert again is None or "已记下" not in again.notice
    assert len(MemoryStore(launch.root).entries()) == 1


def test_a_secret_is_not_written_and_the_user_is_told(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    key = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    context = launch.prompt_hook(event(f"记住 API 密钥是 {key}", tmp_path))
    assert context is not None and "密钥" in context.notice and "没有记" in context.notice
    assert MemoryStore(launch.root).entries() == []


def test_the_switches_decide(tmp_path):
    off = MemoryLaunch(tmp_path / "a", python="py.exe", capture=lambda: False)
    assert off.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path)) is None
    assert MemoryStore(off.root).entries() == []
    other = MemoryLaunch(tmp_path / "b", python="py.exe", enabled=lambda engine: engine != "codex")
    assert other.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path)) is None, "the memory is off for this engine"
    no_recall = MemoryLaunch(tmp_path / "c", python="py.exe", recall=lambda: False)
    context = no_recall.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path))
    assert context is not None and context.notice.startswith("已记下")
    assert no_recall.prompt_hook(event("部署命令是什么", tmp_path, first=True)) is None, "capture does not need recall, and recall stays off"


def test_a_full_memory_says_so(tmp_path, monkeypatch):
    from desktop.core.memory import store as store_module

    monkeypatch.setattr(store_module, "MAX_ACTIVE", 1)
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    launch.prompt_hook(event("记住：第一件事是写测试", tmp_path))
    context = launch.prompt_hook(event("记住：第二件事是提交代码", tmp_path))
    assert context is not None and "已满" in context.notice
