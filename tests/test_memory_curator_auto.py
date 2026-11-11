"""After a turn the application writes what is worth keeping itself, and it waits for the weekly review
(``core/memory/curator`` with a ``ReviewStore``); a note the user asks for in so many words is a lesson too."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from desktop.core.hooks import PromptEvent
from desktop.core.memory import store as store_module
from desktop.core.memory.curator import (
    AUTO_PER_DAY, EXAMPLES, GAP_SECONDS, MAX_NOTES, NEAR_DUPLICATE, RECENT_DAYS, WRITE_AT, Curator, Exchange, build_prompt, confidence_of,
)
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.learn import Learner
from desktop.core.memory.proposals import ProposalStore, digest_of, make
from desktop.core.memory.review import DAY, MAX_PENDING, ReviewStore
from desktop.core.memory.store import ACTIVE, APP, Entry, MemoryStore, project_path

PROJECT = "D:\\work\\demo"
USER = "以后提交信息都用中文，另外这个项目的 e2e 要连跑 20 次才算过"
ANSWER = "好的，我会按这个来：提交信息用中文，改动 e2e 相关的代码之后连跑 20 次再报告结果。" * 2
NOTE = {
    "title": "提交信息用中文", "text": "这个项目的提交信息用中文写。", "kind": "convention", "scope": "project", "tags": ["提交", "commit"],
    "why": "用户明确说了", "confidence": 0.8,
}


def exchange(**more):
    values = {"engine": "chatgpt", "project": PROJECT, "conversation": "测试对话", "row": "cg:1", "user": USER, "assistant": ANSWER, "key": "c1"}
    values.update(more)
    return Exchange(**values)


def reply(*notes):
    return json.dumps({"notes": list(notes)}, ensure_ascii=False)


def note(title, text, confidence=0.8, **more):
    return {"title": title, "text": text, "kind": "fact", "scope": "project", "confidence": confidence, **more}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def auto(tmp_path, answers, **more):
    root = tmp_path / "memory"
    kit = SimpleNamespace(
        store=MemoryStore(root), proposals=ProposalStore(root), review=ReviewStore(root), learner=Learner(root), asked=[], changed=[],
        clock=more.pop("clock", None) or Clock(),
    )

    async def extract(engine, project, prompt):
        kit.asked.append((engine, project, prompt))
        answer = answers.pop(0) if isinstance(answers, list) else answers
        if isinstance(answer, Exception):
            raise answer
        return answer

    kit.curator = Curator(
        kit.store, kit.proposals, extract, clock=kit.clock, changed=lambda: kit.changed.append(1), review=kit.review, learner=kit.learner, **more
    )
    return kit


def run(coro):
    return asyncio.run(coro)


def fake(n, at):
    return Entry(f"m-{n}", f"别的笔记 {n}", f"别的内容 {n}", "global", "", "fact", (), at, at, ACTIVE, 1, APP, APP)


# ----------------------------------------------------------------------------- what is written


def test_a_note_the_model_is_sure_of_is_written_by_the_application_and_waits_for_the_review(tmp_path):
    kit = auto(tmp_path, [reply(NOTE)])
    written = run(kit.curator.consider(exchange()))
    (entry,) = kit.store.entries()
    assert [e.id for e in written] == [entry.id] and kit.changed == [1]
    assert (entry.title, entry.text, entry.kind, entry.scope, entry.project, entry.tags) == ("提交信息用中文", "这个项目的提交信息用中文写。", "convention", "project", project_path(PROJECT), ("提交", "commit"))
    assert entry.source == APP and entry.editor == APP, "the application wrote it, not an agent and not the user"
    (item,) = kit.review.pending()
    assert (item.id, item.engine, item.conversation, item.row, item.why, item.project) == (entry.id, "chatgpt", "测试对话", "cg:1", "用户明确说了", project_path(PROJECT))
    assert (item.llm, item.learned, item.score, item.at) == (0.8, 0.5, 0.8, kit.clock.now), "while the learner knows nothing, the model's word is the score"
    assert kit.proposals.pending() == [], "nothing is suggested"
    prompt = kit.asked[0][2]
    assert '"confidence"' in prompt and "The user kept notes like" not in prompt and USER in prompt


def test_a_note_the_model_is_not_sure_of_is_not_written_but_counted(tmp_path):
    kit = auto(tmp_path, [reply(note("一条", "不太确定的内容。", 0.3), note("二条", "没说多确定的内容。", None), note("三条", "刚好不够的内容。", WRITE_AT - 0.001))])
    assert run(kit.curator.consider(exchange())) == []
    assert kit.store.entries() == [] and kit.review.pending() == [] and kit.changed == []
    assert kit.review.status(kit.clock.now).skipped == 3, "a confidence it did not give is one half: not enough"


def test_a_note_just_sure_enough_is_written(tmp_path):
    kit = auto(tmp_path, [reply(note("刚好够", "刚好够的内容。", WRITE_AT), note("说成字的", "用字写的确定程度。", "90%"))])
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["刚好够", "说成字的"]
    assert sorted(item.llm for item in kit.review.pending()) == [WRITE_AT, 0.9]


def test_what_is_not_fit_to_keep_is_not_written(tmp_path):
    key = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    kit = auto(tmp_path, [reply(note("密钥", f"API key is {key}"), note("", "no title"), note("x" * 200, "too long a title"), note("已经有的一条", "换个说法"), note("别的标题", "这件事已经记过。"), note("好的一条", "确实值得记。", kind="weird", scope="weird"))])
    kit.store.create(title="已经有的一条", text="这件事已经记过。", scope="project", project=PROJECT, kind="fact", by="user")
    written = run(kit.curator.consider(exchange()))
    assert [e.title for e in written] == ["好的一条"], "a secret, an empty title, a long title and what the memory has are out"
    assert (written[0].kind, written[0].scope) == ("fact", "project")


def test_at_most_three_notes_from_one_answer(tmp_path):
    kit = auto(tmp_path, [reply(*(note(f"第 {n} 条", f"内容各不相同 {n}") for n in range(6)))])
    assert len(run(kit.curator.consider(exchange()))) == MAX_NOTES == 3


def test_a_global_note_remembers_the_project_of_its_conversation(tmp_path):
    kit = auto(tmp_path, [reply({**NOTE, "scope": "global"})])
    (entry,) = run(kit.curator.consider(exchange()))
    assert (entry.scope, entry.project) == ("global", "") and kit.review.get(entry.id).project == project_path(PROJECT), "where it can go in the review"


def test_without_a_project_a_project_note_is_global(tmp_path):
    kit = auto(tmp_path, [reply(NOTE)])
    (entry,) = run(kit.curator.consider(exchange(project="")))
    assert (entry.scope, entry.project) == ("global", "") and kit.review.pending()[0].project == ""


def test_words_the_user_dropped_or_refused_are_not_written_again(tmp_path):
    kit = auto(tmp_path, [reply(NOTE, note("拒绝过的", "以前拒绝过的建议。"), note("新的一条", "新的内容。"))])
    kit.review.remember([digest_of(NOTE["title"], NOTE["text"])])
    refused = make(title="拒绝过的", text="以前拒绝过的建议。", scope="project", project=PROJECT, kind="fact", tags=[], engine="codex", conversation="c")
    kit.proposals.add([refused])
    kit.proposals.dismiss(refused.id)
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["新的一条"]


def test_a_near_duplicate_of_a_note_written_lately_is_not_written(tmp_path):
    first = note("e2e 连跑二十次", "改动 e2e 相关的代码之后要整文件连跑二十次才算过")
    again = note("整文件跑二十遍", "改动 e2e 相关的代码之后要整文件连跑二十次才算通过")
    kit = auto(tmp_path, [reply(first), reply(again), reply(again)])
    (written,) = run(kit.curator.consider(exchange()))
    kit.store.forget(written.id, by="user")
    kit.clock.now += 13 * DAY
    assert run(kit.curator.consider(exchange())) == [], "the same thing in other words, which the user has just forgotten"
    kit.clock.now += 2 * DAY
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["整文件跑二十遍"], "two weeks on: the words may come again"
    assert (NEAR_DUPLICATE, RECENT_DAYS) == (0.7, 14)


def test_a_near_duplicate_of_a_note_in_the_memory_is_not_written(tmp_path):
    kit = auto(tmp_path, [reply(note("整文件跑二十遍", "改动 e2e 相关的代码之后要整文件连跑二十次才算通过"), note("Step 2 构建", "先构建前端页面，然后启动应用"))])
    kit.store.create(title="e2e 的规矩", text="改动 e2e 相关的代码之后要整文件连跑二十次才算过", scope="project", project=PROJECT, kind="convention", by="user")
    kit.store.create(title="Step 1 构建", text="先构建前端页面再启动应用", scope="global", kind="convention", by="user")
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["Step 2 构建"], "another number in the title is another thing"


def test_two_near_duplicates_in_one_answer_make_one_note(tmp_path):
    kit = auto(tmp_path, [reply(note("e2e 连跑二十次", "改动 e2e 相关的代码之后要整文件连跑二十次才算过"), note("整文件跑二十遍", "改动 e2e 相关的代码之后要整文件连跑二十次才算通过"))])
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["e2e 连跑二十次"]


# ----------------------------------------------------------------------------- how many


def test_no_more_than_so_many_notes_a_day(tmp_path):
    kit = auto(tmp_path, [reply(*(note(f"今天第 {n} 条", f"今天的内容各不相同 {n}") for n in range(3))), reply(note("明天的一条", "明天的内容。"))])
    for n in range(AUTO_PER_DAY - 1):
        kit.review.add(fake(n, kit.clock.now - 3600), score=0.6, llm=0.6, learned=0.5, now=kit.clock.now - 3600)
    assert len(run(kit.curator.consider(exchange()))) == 1, "one more is all the day allows"
    kit.clock.now += GAP_SECONDS + 1
    assert run(kit.curator.consider(exchange())) == [] and len(kit.asked) == 1, "no room: not even asked"
    kit.clock.now += DAY
    assert [e.title for e in run(kit.curator.consider(exchange()))] == ["明天的一条"] and len(kit.asked) == 2


def test_nothing_more_is_written_while_too_many_wait_for_the_user(tmp_path):
    kit = auto(tmp_path, [reply(NOTE)])
    for n in range(MAX_PENDING):
        kit.review.add(fake(n, kit.clock.now - 3 * DAY), score=0.6, llm=0.6, learned=0.5, now=kit.clock.now - 3 * DAY)
    assert run(kit.curator.consider(exchange())) == [] and kit.asked == []


def test_a_full_memory_writes_nothing_and_says_nothing(tmp_path, monkeypatch):
    kit = auto(tmp_path, [reply(NOTE)])
    monkeypatch.setattr(store_module, "MAX_ACTIVE", 0)
    assert run(kit.curator.consider(exchange())) == [] and kit.review.pending() == [] and kit.changed == []


# ----------------------------------------------------------------------------- what was learned


def test_the_prompt_shows_what_the_user_kept_and_dropped(tmp_path):
    kit = auto(tmp_path, [reply(), reply()])
    run(kit.curator.consider(exchange()))
    assert "The user kept notes like" not in kit.asked[0][2] and "The user dropped notes like" not in kit.asked[0][2]
    kit.learner.learn({"title": "回答用中文", "text": "用户希望回答\n都用中文。", "kind": "preference", "scope": "global"}, True)
    kit.learner.learn({"title": "本项目的端口", "text": "开发端口 8080。", "kind": "fact", "scope": "project", "project": PROJECT}, True)
    kit.learner.learn({"title": "别的项目的端口", "text": "别处的端口。", "kind": "fact", "scope": "project", "project": "E:\\other"}, True)
    kit.learner.learn({"title": "修了拼写错误", "text": "改了一个错字。" * 40, "kind": "fact", "scope": "global"}, False)
    kit.clock.now += GAP_SECONDS + 1
    run(kit.curator.consider(exchange()))
    prompt = kit.asked[1][2]
    assert "The user kept notes like:\n- 本项目的端口: 开发端口 8080。\n- 回答用中文: 用户希望回答 都用中文。\n" in prompt
    assert "The user dropped notes like:\n- 修了拼写错误: 改了一个错字。" in prompt and "…" in prompt, "a long text is cut"
    assert "别的项目的端口" not in prompt, "another project's notes stay with it"
    assert prompt.index("The user kept notes like") < prompt.index('"confidence"') < prompt.index("<exchange>")
    assert EXAMPLES == 5


def test_once_the_learner_knows_enough_it_has_as_much_say_as_the_model(tmp_path):
    kit = auto(tmp_path, [reply(note("omega sigma", "theta kappa notes", 0.8), note("alpha beta", "gamma delta notes", 0.4))])
    keep = {"title": "alpha beta", "text": "gamma delta", "kind": "fact", "scope": "project", "engine": "chatgpt"}
    drop = {"title": "omega sigma", "text": "theta kappa", "kind": "fact", "scope": "project", "engine": "chatgpt"}
    kit.learner.learn_all([(keep, True)] * 15 + [(drop, False)] * 15)
    assert kit.learner.ready
    written = run(kit.curator.consider(exchange()))
    assert [e.title for e in written] == ["alpha beta"], "what the user dropped before is not written however sure the model is"
    (item,) = kit.review.pending()
    assert item.llm == 0.4 and item.learned == kit.learner.score(item) > 0.7 and item.score == pytest.approx(0.5 * 0.4 + 0.5 * item.learned)
    assert kit.review.status(kit.clock.now).skipped == 1


def test_a_curator_with_a_review_and_no_learner_learns_next_to_the_review(tmp_path):
    root = tmp_path / "memory"
    curator = Curator(MemoryStore(root), ProposalStore(root), lambda *a: None, review=ReviewStore(root))
    assert curator.learner is not None and curator.learner.path == root / "learned.json"
    legacy = Curator(MemoryStore(root), ProposalStore(root), lambda *a: None)
    assert legacy.review is None and legacy.learner is None


# ----------------------------------------------------------------------------- the old way is still there


def test_without_a_review_it_only_suggests_as_before(tmp_path):
    root = tmp_path / "memory"
    store, proposals, learner = MemoryStore(root), ProposalStore(root), Learner(root)
    asked = []

    async def extract(engine, project, prompt):
        asked.append(prompt)
        return reply(NOTE)

    learner.learn({"title": "回答用中文", "text": "用户希望回答都用中文。", "kind": "preference", "scope": "global"}, True)
    curator = Curator(store, proposals, extract, clock=Clock(), learner=learner)
    run(curator.consider(exchange()))
    assert [p.title for p in proposals.pending()] == ["提交信息用中文"] and store.entries() == []
    assert asked == [build_prompt(exchange(), [])], "the prompt of before: no confidence, no examples"
    assert '"confidence"' not in asked[0] and not (root / "review.json").exists()


# ----------------------------------------------------------------------------- never in the way


def test_a_request_that_fails_is_only_logged_and_a_cancel_is_not_swallowed(tmp_path, caplog):
    kit = auto(tmp_path, [RuntimeError("boom"), reply(NOTE)])
    with caplog.at_level("WARNING"):
        assert run(kit.curator.consider(exchange())) == []
    assert "could not ask" in caplog.text
    kit.clock.now += GAP_SECONDS + 1
    assert len(run(kit.curator.consider(exchange()))) == 1

    async def scenario():
        async def never(engine, project, prompt):
            await asyncio.sleep(60)

        root = tmp_path / "cancel"
        curator = Curator(MemoryStore(root), ProposalStore(root), never, review=ReviewStore(root))
        task = asyncio.create_task(curator.consider(exchange()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert curator._busy is False

    run(scenario())


def test_a_review_that_cannot_be_read_asks_nothing(tmp_path, caplog, monkeypatch):
    kit = auto(tmp_path, [reply(NOTE)])

    def broken(since):
        raise OSError("disk gone")

    monkeypatch.setattr(kit.review, "counts", broken)
    with caplog.at_level("WARNING"):
        assert run(kit.curator.consider(exchange())) == []
    assert kit.asked == [] and "could not read the review" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.8, 0.8), (1, 1.0), (0, 0.0), (1.7, 1.0), (-0.2, 0.0), ("0.65", 0.65), (" 80% ", 0.8), ("high", 0.5), ("", 0.5), (None, 0.5), (True, 0.5),
     (float("nan"), 0.5), (float("inf"), 0.5), ("inf", 0.5), ([0.9], 0.5)],
)
def test_how_sure_the_model_said_it_is(value, expected):
    assert confidence_of(value) == pytest.approx(expected)


# ----------------------------------------------------------------------------- what the user asks for in so many words


def event(text, project, engine="codex"):
    return PromptEvent(engine, project, text)


def test_a_note_the_user_asked_for_is_a_lesson_when_the_learner_listens(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe", learn=lambda: True)
    context = launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path))
    assert context is not None and context.notice.startswith("已记下")
    assert launch.learner.counts() == (1, 0)
    (example,) = launch.learner.examples(True)
    assert (example.title, example.kind, example.scope) == ("部署命令是 make ship-it", "fact", "project")
    assert launch.learner.score({"title": "部署命令", "text": "make ship-it", "kind": "fact", "scope": "project", "engine": "codex"}) > 0.5
    assert launch.review.count_pending() == 0, "the user said it: nothing to review"
    launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path))
    assert launch.learner.counts() == (1, 0), "a note that is there already teaches nothing new"


def test_by_default_a_note_the_user_asked_for_teaches_nothing(tmp_path):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    assert launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path)).notice.startswith("已记下")
    assert not (tmp_path / "memory" / "learned.json").exists() and launch.learner.labels == 0


def test_a_learner_that_fails_does_not_stop_the_note(tmp_path, caplog, monkeypatch):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe", learn=lambda: True)

    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(launch.learner, "learn", broken)
    with caplog.at_level("WARNING"):
        context = launch.prompt_hook(event("记住：部署命令是 make ship-it", tmp_path))
    assert context.notice.startswith("已记下") and len(MemoryStore(launch.root).entries()) == 1 and "could not learn" in caplog.text
