"""After a turn the engine is asked what is worth keeping; the answers wait as suggestions (``core/memory/curator``, ``proposals``)."""

from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.memory import curator as curator_module
from desktop.core.memory.curator import GAP_SECONDS, PER_HOUR, Curator, Exchange, build_prompt, parse_reply
from desktop.core.memory.proposals import MAX_PENDING, ProposalStore, digest_of, make
from desktop.core.memory.store import MemoryStore

PROJECT = "D:\\work\\demo"
USER = "以后提交信息都用中文，另外这个项目的 e2e 要连跑 20 次才算过"
ANSWER = "好的，我会按这个来：提交信息用中文，改动 e2e 相关的代码之后连跑 20 次再报告结果。" * 2


def exchange(**more):
    values = {"engine": "chatgpt", "project": PROJECT, "conversation": "测试对话", "row": "cg:1", "user": USER, "assistant": ANSWER, "key": "c1"}
    values.update(more)
    return Exchange(**values)


def reply(*notes):
    return json.dumps({"notes": list(notes)}, ensure_ascii=False)


NOTE = {"title": "提交信息用中文", "text": "这个项目的提交信息用中文写。", "kind": "convention", "scope": "project", "tags": ["提交", "commit"], "why": "用户明确说了"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def make_curator(tmp_path, answers, **more):
    store = MemoryStore(tmp_path / "memory")
    proposals = ProposalStore(tmp_path / "memory")
    asked = []
    clock = more.pop("clock", None) or Clock()
    changed = []

    async def extract(engine, project, prompt):
        asked.append((engine, project, prompt))
        answer = answers.pop(0) if isinstance(answers, list) else answers
        if isinstance(answer, Exception):
            raise answer
        return answer

    curator = Curator(store, proposals, extract, clock=clock, changed=lambda: changed.append(1), **more)
    return curator, store, proposals, asked, changed, clock


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- the prompt and the reply


def test_the_prompt_holds_the_exchange_the_known_titles_and_the_format():
    prompt = build_prompt(exchange(), ["已有的一条", "另一条"])
    assert "demo" in prompt and "- 已有的一条" in prompt and "- 另一条" in prompt
    assert f"USER:\n{USER}" in prompt and "ASSISTANT:" in prompt and '"notes"' in prompt and "at most 3 notes" in prompt
    assert "(none yet)" in build_prompt(exchange(), [])
    assert "(no project)" in build_prompt(exchange(project=""), [])


def test_a_long_exchange_is_cut_to_the_room_there_is():
    prompt = build_prompt(exchange(user="问" * 20000, assistant="答" * 20000), [])
    assert len(prompt) < curator_module.EXCHANGE_CHARS + 3000
    assert "…" in prompt


def test_the_reply_is_read_whatever_it_is_wrapped_in():
    note = {"title": "t", "text": "x"}
    assert parse_reply(json.dumps({"notes": [note]})) == [note]
    assert parse_reply("```json\n" + json.dumps({"notes": [note]}) + "\n```") == [note]
    assert parse_reply("Here you go: " + json.dumps({"notes": [note]}) + " Hope that helps") == [note]
    assert parse_reply(json.dumps([note])) == [note]
    assert parse_reply('{"notes": []}') == []
    for rubbish in ("", "nothing worth keeping", "{not json", '{"notes": "oops"}', "[1, 2]", "null"):
        assert parse_reply(rubbish) == [], rubbish


# ----------------------------------------------------------------------------- what is kept


def test_a_note_in_the_reply_becomes_a_suggestion_with_where_it_came_from(tmp_path):
    curator, store, proposals, asked, changed, _ = make_curator(tmp_path, [reply(NOTE)])
    kept = run(curator.consider(exchange()))
    assert len(kept) == 1 and changed == [1]
    (waiting,) = proposals.pending()
    assert (waiting.title, waiting.text, waiting.kind, waiting.scope, waiting.project) == ("提交信息用中文", "这个项目的提交信息用中文写。", "convention", "project", PROJECT)
    assert (waiting.engine, waiting.conversation, waiting.row, waiting.why) == ("chatgpt", "测试对话", "cg:1", "用户明确说了")
    assert waiting.tags == ("提交", "commit") and store.entries() == [], "nothing is written to the memory without the user's word"
    assert asked[0][0] == "chatgpt" and asked[0][1] == PROJECT


def test_an_empty_answer_a_refusal_and_garbage_leave_nothing(tmp_path):
    for answer in (reply(), None, "", "I have nothing to add.", "{broken"):
        curator, _, proposals, _, changed, _ = make_curator(tmp_path / str(abs(hash(str(answer)))), [answer])
        assert run(curator.consider(exchange())) == []
        assert proposals.pending() == [] and changed == []


def test_what_is_not_fit_to_keep_is_left_out(tmp_path):
    key = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    curator, store, proposals, _, _, _ = make_curator(
        tmp_path,
        [
            reply(
                {"title": "密钥", "text": f"API key is {key}", "kind": "fact"},
                {"title": "", "text": "no title"},
                {"title": "no text", "text": "  "},
                {"title": "x" * 200, "text": "too long a title"},
                {"title": "好的一条", "text": "确实值得记。", "kind": "weird", "scope": "weird"},
                "not an object",
                {"title": "第四条", "text": "超过三条的不要"},
            )
        ],
    )
    store.create(title="已经有的一条", text="这件事已经记过。", scope="project", project=PROJECT, kind="fact", by="user")
    run(curator.consider(exchange()))
    titles = [p.title for p in proposals.pending()]
    assert titles == ["好的一条", "第四条"], "a secret, an empty one, a long title and a non-object are out"
    kept = next(p for p in proposals.pending() if p.title == "好的一条")
    assert (kept.kind, kept.scope) == ("fact", "project"), "an unknown kind or scope becomes the plain one"


def test_at_most_three_notes_are_kept_from_one_answer(tmp_path):
    notes = [{"title": f"第 {n} 条", "text": f"内容各不相同 {n}"} for n in range(6)]
    curator, _, proposals, _, _, _ = make_curator(tmp_path, [reply(*notes)])
    run(curator.consider(exchange()))
    assert len(proposals.pending()) == 3


def test_what_the_memory_already_says_is_not_suggested_again(tmp_path):
    curator, store, proposals, _, _, _ = make_curator(tmp_path, [reply({"title": "提交信息用中文", "text": "换了说法的同一件事", "kind": "convention"})])
    store.create(title="提交信息用中文", text="这个项目的提交信息用中文写。", scope="project", project=PROJECT, kind="convention", by="user")
    run(curator.consider(exchange()))
    assert proposals.pending() == []
    other, store2, proposals2, _, _, _ = make_curator(tmp_path / "b", [reply({"title": "别的标题", "text": "这个项目的提交信息用中文写。"})])
    store2.create(title="提交信息用中文", text="这个项目的提交信息用中文写。", scope="project", project=PROJECT, kind="convention", by="user")
    run(other.consider(exchange()))
    assert proposals2.pending() == [], "the same words under another title"


def test_a_suggestion_the_user_dismissed_does_not_come_back(tmp_path):
    curator, _, proposals, _, _, clock = make_curator(tmp_path, [reply(NOTE), reply(NOTE)])
    run(curator.consider(exchange()))
    (waiting,) = proposals.pending()
    assert proposals.dismiss(waiting.id) is True
    clock.now += GAP_SECONDS + 1
    run(curator.consider(exchange()))
    assert proposals.pending() == []


def test_the_same_suggestion_is_not_waiting_twice(tmp_path):
    curator, _, proposals, _, changed, clock = make_curator(tmp_path, [reply(NOTE), reply(NOTE)])
    run(curator.consider(exchange()))
    clock.now += GAP_SECONDS + 1
    assert run(curator.consider(exchange())) == []
    assert len(proposals.pending()) == 1 and changed == [1]


def test_with_no_project_a_project_note_becomes_a_global_one(tmp_path):
    curator, _, proposals, _, _, _ = make_curator(tmp_path, [reply(NOTE)])
    run(curator.consider(exchange(project="")))
    assert [(p.scope, p.project) for p in proposals.pending()] == [("global", "")]


# ----------------------------------------------------------------------------- when to ask


def test_a_turn_that_said_little_or_an_engine_that_is_off_is_not_asked_about(tmp_path):
    curator, _, _, asked, _, _ = make_curator(tmp_path, reply(NOTE))
    for short in (exchange(user="好"), exchange(assistant="好的"), exchange(user="   ")):
        assert curator.worth_asking(short) is False
        assert run(curator.consider(short)) == []
    off, _, _, asked_off, _, _ = make_curator(tmp_path / "b", reply(NOTE), enabled=lambda engine: engine != "chatgpt")
    assert run(off.consider(exchange())) == []
    assert asked == [] and asked_off == []


def test_one_request_per_conversation_in_a_while_and_one_at_a_time(tmp_path):
    curator, _, _, asked, _, clock = make_curator(tmp_path, [reply(), reply(), reply(), reply()])
    run(curator.consider(exchange()))
    run(curator.consider(exchange()))
    assert len(asked) == 1, "the same conversation again at once"
    run(curator.consider(exchange(key="c2")))
    assert len(asked) == 2, "another conversation has its own turn"
    clock.now += GAP_SECONDS + 1
    run(curator.consider(exchange()))
    assert len(asked) == 3

    async def two_at_once():
        gate = asyncio.Event()
        order = []

        async def slow(engine, project, prompt):
            order.append("start")
            await gate.wait()
            order.append("end")
            return reply()

        c = Curator(MemoryStore(tmp_path / "x"), ProposalStore(tmp_path / "x"), slow, clock=clock)
        first = asyncio.create_task(c.consider(exchange(key="a")))
        await asyncio.sleep(0)
        assert await c.consider(exchange(key="b")) == [], "one request in flight at a time"
        gate.set()
        await first
        assert order == ["start", "end"]

    run(two_at_once())


def test_no_more_than_so_many_requests_an_hour(tmp_path):
    curator, _, _, asked, _, clock = make_curator(tmp_path, reply())
    for number in range(PER_HOUR + 5):
        clock.now += GAP_SECONDS + 1
        run(curator.consider(exchange()))
    assert len(asked) == PER_HOUR
    clock.now += 3601
    run(curator.consider(exchange()))
    assert len(asked) == PER_HOUR + 1, "an hour later it asks again"


def test_a_request_that_fails_is_only_logged_and_the_next_one_goes(tmp_path, caplog):
    curator, _, proposals, asked, _, clock = make_curator(tmp_path, [RuntimeError("boom"), reply(NOTE)])
    with caplog.at_level("WARNING"):
        assert run(curator.consider(exchange())) == []
    assert "could not ask" in caplog.text
    clock.now += GAP_SECONDS + 1
    run(curator.consider(exchange()))
    assert len(proposals.pending()) == 1 and len(asked) == 2, "not stuck busy after an error"


def test_a_cancelled_request_is_not_swallowed(tmp_path):
    async def scenario():
        async def never(engine, project, prompt):
            await asyncio.sleep(60)

        c = Curator(MemoryStore(tmp_path / "m"), ProposalStore(tmp_path / "m"), never)
        task = asyncio.create_task(c.consider(exchange()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c._busy is False

    run(scenario())


# ----------------------------------------------------------------------------- the waiting list


def test_the_file_survives_a_restart_and_a_damaged_one_is_nothing(tmp_path):
    first = ProposalStore(tmp_path)
    kept = first.add([make(title="一", text="第一条", scope="global", project="", kind="fact", tags=[], engine="codex", conversation="c")])
    assert [p.title for p in ProposalStore(tmp_path).pending()] == ["一"] and ProposalStore(tmp_path).count() == 1
    (tmp_path / "proposals.json").write_text("{not json", encoding="utf-8")
    assert ProposalStore(tmp_path).pending() == [] and ProposalStore(tmp_path).count() == 0
    (tmp_path / "proposals.json").write_text(json.dumps({"pending": [{"id": "x"}, 5, {"id": "p-1", "title": "好", "text": "t", "scope": "bad", "kind": "fact"}], "dismissed": [1, "d"]}), encoding="utf-8")
    assert ProposalStore(tmp_path).pending() == [], "entries that cannot be read are dropped"
    assert kept


def test_the_oldest_suggestions_go_when_there_are_too_many(tmp_path):
    store = ProposalStore(tmp_path)
    for number in range(MAX_PENDING + 5):
        store.add([make(title=f"标题 {number}", text=f"内容 {number} 各不相同", scope="global", project="", kind="fact", tags=[], engine="codex", conversation="c", now=1000.0 + number)])
    pending = store.pending()
    assert len(pending) == MAX_PENDING
    assert pending[0].title == f"标题 {MAX_PENDING + 4}" and pending[-1].title == "标题 5", "newest first, the five oldest are gone"


def test_edits_apply_only_to_the_fields_that_may_change(tmp_path):
    store = ProposalStore(tmp_path)
    (item,) = store.add([make(title="原来的", text="原来的内容", scope="project", project=PROJECT, kind="fact", tags=["a"], engine="codex", conversation="c")])
    edited = store.edited(item.id, title="改后的", kind="pitfall", id="p-evil", engine="droid", project="E:\\other")
    assert edited is not None and (edited.title, edited.kind, edited.id, edited.engine) == ("改后的", "pitfall", item.id, "codex")
    assert edited.project == "E:\\other", "the project is the user's to move it to"
    assert store.edited("p-nothing", title="x") is None


def test_dismissing_all_remembers_all_of_them(tmp_path):
    store = ProposalStore(tmp_path)
    items = [make(title=f"标题{n}", text=f"内容{n}独一无二", scope="global", project="", kind="fact", tags=[], engine="codex", conversation="c") for n in range(3)]
    store.add(items)
    assert store.dismiss_all() == 3 and store.pending() == [] and store.dismiss_all() == 0
    assert store.add(items) == [], "none comes back"


def test_the_fingerprint_ignores_case_spacing_and_punctuation():
    assert digest_of("Use Tabs", "Always, use tabs!") == digest_of("use  tabs", "always use tabs")
    assert digest_of("a", "b") != digest_of("a", "c")
    assert digest_of("提交信息用中文。", "ok") == digest_of("提交信息 用中文", "OK")
