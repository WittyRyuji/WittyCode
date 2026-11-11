"""The notes the application wrote on its own wait for one look a week; the user's word teaches the learner
(``core/memory/review``, and the page's side of it in ``MemoryBridge``)."""

from __future__ import annotations

import json
import threading

import pytest

from desktop.core.memory import store as store_module
from desktop.core.memory.bridge import MESSAGES, MemoryBridge, explain
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.learn import MIN_LABELS, Learner
from desktop.core.memory.proposals import ProposalStore, digest_of, make
from desktop.core.memory.review import (
    DAY, DROPPED, DUE_MIN, KEEP_DECIDED_DAYS, KEPT, MAX_DIGESTS, MAX_HISTORY, PENDING, REVIEW_EVERY_DAYS, REVIEW_SIZE, ReviewStore, combined,
    migrate_proposals,
)
from desktop.core.memory.store import ACTIVE, APP, Entry, MemoryRejected, MemoryStore, agent, project_path

PROJECT = "D:\\work\\demo"
NOW = 1_000_000.0
WEEK = REVIEW_EVERY_DAYS * DAY


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def kit(tmp_path):
    root = tmp_path / "memory"
    return MemoryStore(root), ReviewStore(root), Learner(root)


def write(store, review, title, text, *, at=NOW, kind="convention", scope="global", engine="codex", tags=(), llm=0.7, learned=0.5, score=None, project=""):
    """A note written by the application, up for review (``project``: the conversation's, for a global note)."""
    entry = store.create(title=title, text=text, scope=scope, project=PROJECT if scope == "project" else "", kind=kind, tags=list(tags), by=APP)
    review.add(
        entry, engine=engine, conversation="测试对话", row="cg:1", why="用户明确说了", project=project, score=llm if score is None else score, llm=llm,
        learned=learned, now=at,
    )
    return entry


def fake(n, *, title=None, text=None, scope="global", kind="fact"):
    """A note the memory never had (for what needs no store)."""
    return Entry(f"m-{n}", title or f"笔记 {n}", text or f"内容 {n}", scope, PROJECT if scope == "project" else "", kind, (), NOW, NOW, ACTIVE, 1, APP, APP)


def teach(learner, count=30):
    """A learner that has seen ``count`` notes kept and as many dropped, each kind easy to tell from the other (all of
    them global: the scope says nothing)."""
    keep = {"title": "alpha beta", "text": "gamma delta", "kind": "convention", "scope": "global", "engine": "codex", "tags": ["keepme"]}
    drop = {"title": "omega sigma", "text": "theta kappa", "kind": "fact", "scope": "global", "engine": "devin", "tags": ["dropme"]}
    learner.learn_all([(keep, True)] * count + [(drop, False)] * count, now=NOW)


# ----------------------------------------------------------------------------- waiting


def test_a_note_the_application_wrote_waits_with_where_it_came_from(tmp_path):
    store, review, _ = kit(tmp_path)
    entry = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。", scope="project", tags=["提交"], llm=0.8, learned=0.4, score=0.6)
    (item,) = review.pending()
    assert (item.id, item.title, item.text, item.tags, item.kind, item.scope, item.project) == (
        entry.id, "提交信息用中文", "这个项目的提交信息用中文写。", ("提交",), "convention", "project", project_path(PROJECT)
    )
    assert (item.engine, item.conversation, item.row, item.why) == ("codex", "测试对话", "cg:1", "用户明确说了")
    assert (item.score, item.llm, item.learned, item.at, item.state, item.by, item.decided_at) == (0.6, 0.8, 0.4, NOW, PENDING, "", 0.0)
    assert item.digest == digest_of(entry.title, entry.text) and review.get(entry.id) == item and review.count_pending() == 1
    again = review.add(entry, score=0.1, llm=0.1, learned=0.1, now=NOW + 5)
    assert again == item and review.count_pending() == 1, "a note that waits already stays as it was"
    data = json.loads((tmp_path / "memory" / "review.json").read_text(encoding="utf-8"))
    assert set(data) == {"version", "items", "last_review", "history", "skipped", "digests"}
    assert set(data["items"][entry.id]) >= {"title", "kind", "scope", "project", "engine", "conversation", "row", "why", "score", "llm", "learned", "at", "state", "by", "decided_at"}


def test_the_newest_wait_first_and_the_counts_follow(tmp_path):
    review = ReviewStore(tmp_path)
    for n, at in enumerate((NOW, NOW + 30, NOW + 10)):
        review.add(fake(n), score=0.6, llm=0.6, learned=0.5, now=at)
    assert [item.id for item in review.pending()] == ["m-1", "m-2", "m-0"]
    assert review.counts(NOW + 10) == (2, 3) and review.counts(NOW + 31) == (0, 3)
    assert {item.id for item in review.recent(NOW + 10)} == {"m-1", "m-2"}
    review.skip(2)
    review.skip(0)
    review.skip()
    assert review.status(NOW).skipped == 3


def test_reviews_of_one_file_take_turns(tmp_path):
    a, b = ReviewStore(tmp_path), ReviewStore(tmp_path)
    assert a._lock is b._lock

    def add(review, first):
        for n in range(first, first + 15):
            review.add(fake(n), score=0.6, llm=0.6, learned=0.5, now=NOW)

    threads = [threading.Thread(target=add, args=(review, first)) for review, first in ((a, 0), (b, 100))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert ReviewStore(tmp_path).count_pending() == 30, "no note is lost"


def test_a_damaged_file_is_an_empty_review(tmp_path):
    path = tmp_path / "review.json"
    for rubbish in ("{not json", "[]", json.dumps({"items": {"m-1": {"title": ""}, "m-2": 5, "m-3": {"title": "t", "state": "odd"}}, "last_review": "x", "skipped": -1, "digests": [1, "d"], "history": [1, {"at": 1}]})):
        path.write_text(rubbish, encoding="utf-8")
        review = ReviewStore(tmp_path)
        assert review.pending() == [] and review.status(NOW) == (False, 0, 0.0, 0.0, 0), rubbish
    assert review.digests() == {"d"} and review.history() == [{"at": 1}]
    path.write_text(json.dumps({"items": {"m-1": {"title": "好", "score": "high", "llm": 7, "learned": -2, "at": "now"}}}), encoding="utf-8")
    (item,) = ReviewStore(tmp_path).pending()
    assert (item.score, item.llm, item.learned, item.at, item.kind, item.scope) == (0.5, 1.0, 0.0, 0.0, "fact", "project"), "what cannot be read is the plain value"


# ----------------------------------------------------------------------------- when a review is due


def test_fewer_than_due_min_notes_are_never_a_review(tmp_path):
    review = ReviewStore(tmp_path)
    for n in range(DUE_MIN - 1):
        review.add(fake(n), score=0.6, llm=0.6, learned=0.5, now=NOW)
    assert review.due(NOW + 100 * WEEK) is False
    review.add(fake(9), score=0.6, llm=0.6, learned=0.5, now=NOW + 3 * DAY)
    assert review.due(NOW + WEEK - 1) is False and review.due(NOW + WEEK) is True, "the oldest has waited a week"
    assert review.status(NOW).next == NOW + WEEK


def test_the_week_is_counted_from_the_last_review(tmp_path):
    store, review, learner = kit(tmp_path)
    for n in range(DUE_MIN):
        write(store, review, f"笔记 {n}", f"内容 {n}", at=NOW)
    review.apply([], [], store=store, learner=learner, now=NOW + 10 * DAY)
    assert review.due(NOW + 10 * DAY + WEEK - 1) is False and review.due(NOW + 10 * DAY + WEEK) is True
    status = review.status(NOW + 11 * DAY)
    assert (status.last, status.next, status.pending) == (NOW + 10 * DAY, NOW + 10 * DAY + WEEK, DUE_MIN)


def test_so_many_waiting_cannot_wait_a_week(tmp_path):
    review = ReviewStore(tmp_path)
    for n in range(2 * REVIEW_SIZE - 1):
        review.add(fake(n), score=0.6, llm=0.6, learned=0.5, now=NOW)
    assert review.due(NOW) is False
    review.add(fake(999), score=0.6, llm=0.6, learned=0.5, now=NOW)
    assert review.due(NOW) is True


# ----------------------------------------------------------------------------- the batch


def test_a_batch_is_the_least_sure_first_and_the_oldest_among_equals(tmp_path):
    review = ReviewStore(tmp_path)
    for n, (score, at) in enumerate(((0.9, NOW), (0.6, NOW + 2), (0.6, NOW + 1), (0.8, NOW), (0.55, NOW + 5))):
        review.add(fake(n), score=score, llm=score, learned=0.5, now=at)
    assert [item.id for item in review.batch(NOW, 3)] == ["m-4", "m-2", "m-1"]
    assert [item.id for item in review.batch(NOW)] == ["m-4", "m-2", "m-1", "m-3", "m-0"]
    assert REVIEW_SIZE == 50 and len(review.batch(NOW, 0)) == 0


def test_a_batch_shows_the_words_as_they_are_now_and_settles_what_was_forgotten(tmp_path):
    store, review, learner = kit(tmp_path)
    forgotten = write(store, review, "临时调试输出", "在 loop.py 里加了一行 print。", kind="fact")
    changed = write(store, review, "测试命令", "测试用 pytest 跑。")
    by_agent = write(store, review, "旧端口", "开发服务器用 8080。", kind="fact")
    never = fake(7, title="不在记忆里的一条")
    review.add(never, score=0.6, llm=0.6, learned=0.5, now=NOW)
    store.forget(forgotten.id, by="user")
    store.update(changed.id, by="user", text="测试用 .venv 里的 pytest 跑。", kind="fact")
    store.forget(by_agent.id, by=agent("codex", "r1"))
    shown = review.batch(NOW + WEEK, store=store, learner=learner)
    assert [item.id for item in shown] == [changed.id]
    assert (shown[0].text, shown[0].kind) == ("测试用 .venv 里的 pytest 跑。", "fact") and review.get(changed.id).text == "测试用 pytest 跑。", "the file keeps the words as written"
    assert (review.get(forgotten.id).state, review.get(forgotten.id).by, review.get(forgotten.id).decided_at) == (DROPPED, "user", NOW + WEEK)
    assert (review.get(by_agent.id).state, review.get(by_agent.id).by) == (DROPPED, "agent")
    assert (review.get(never.id).state, review.get(never.id).by) == (DROPPED, "auto")
    assert learner.counts() == (0, 1) and learner.examples(False)[0].title == "临时调试输出", "only the user's drop is a lesson"
    assert digest_of(forgotten.title, forgotten.text) in review.digests() and digest_of(by_agent.title, by_agent.text) in review.digests()
    assert review.count_pending() == 1


def test_without_a_learner_a_batch_settles_nothing(tmp_path):
    store, review, learner = kit(tmp_path)
    forgotten = write(store, review, "临时调试输出", "在 loop.py 里加了一行 print。")
    store.forget(forgotten.id, by="user")
    assert review.batch(NOW, store=store) == [] and review.get(forgotten.id).state == PENDING, "the lesson waits for a learner"


def test_a_batch_scores_again_with_what_the_learner_knows_now(tmp_path):
    store, review, learner = kit(tmp_path)
    teach(learner)
    liked = write(store, review, "alpha beta", "gamma delta notes", tags=["keepme"], llm=0.6, learned=0.5)
    disliked = write(store, review, "omega sigma", "theta kappa notes", kind="fact", scope="project", engine="devin", tags=["dropme"], llm=0.6, learned=0.5)
    shown = review.batch(NOW, store=store, learner=learner)
    assert [item.id for item in shown] == [disliked.id, liked.id]
    low, high = shown
    assert low.learned == learner.score(low) and low.score == combined(0.6, low.learned, True) < 0.5 < high.score
    assert review.get(liked.id).score == high.score, "the new scores are kept"


def test_a_batch_picks_by_what_the_learner_knows_now(tmp_path):
    store, review, learner = kit(tmp_path)
    teach(learner)
    write(store, review, "部署命令", "部署用 make ship-it。", kind="pitfall", engine="chatgpt", llm=0.6)
    write(store, review, "alpha beta", "gamma delta notes", tags=["keepme"], llm=0.7)
    disliked = write(store, review, "omega sigma", "theta kappa notes", kind="fact", engine="devin", tags=["dropme"], llm=0.8)
    assert [item.id for item in review.batch(NOW, 1, store=store, learner=learner)] == [disliked.id], "the surest of the model, the least sure now"


# ----------------------------------------------------------------------------- the user's word


def test_the_users_word_keeps_drops_and_edits(tmp_path):
    store, review, learner = kit(tmp_path)
    a = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。")
    b = write(store, review, "临时调试输出", "在 loop.py 里加了一行 print。", kind="fact")
    c = write(store, review, "测试命令", "测试用 pytest 跑。")
    d = write(store, review, "这次没看的一条", "这一条这次没有看。")
    review.skip(4)
    edits = {
        c.id: {"title": "测试用 pytest", "text": "测试用 .venv 里的 pytest 跑。", "kind": "fact", "tags": ["测试"]}, b.id: {"title": "改了也不算"},
        a.id: {"title": "提交信息用中文", "kind": "convention"},
    }
    done = review.apply([a.id, c.id], [b.id], edits, store=store, learner=learner, now=NOW + DAY)
    assert done == {"kept": 2, "dropped": 1, "autoKept": 0, "autoDropped": 0, "labels": 3, "pending": 1, "next": int(NOW + DAY + WEEK)}
    assert (review.get(a.id).state, review.get(a.id).by, review.get(a.id).decided_at) == (KEPT, "user", NOW + DAY)
    assert (review.get(b.id).state, review.get(b.id).by) == (DROPPED, "user") and review.get(d.id).state == PENDING
    forgotten = store.read(b.id)
    assert forgotten.status == "forgotten" and forgotten.editor == "user" and forgotten.title == "临时调试输出", "forgotten as it was, and it can be restored"
    edited = store.read(c.id)
    assert (edited.title, edited.text, edited.kind, edited.tags, edited.editor) == ("测试用 pytest", "测试用 .venv 里的 pytest 跑。", "fact", ("测试",), "user")
    assert store.read(a.id).rev == 1, "an edit that changes nothing makes no new revision"
    assert digest_of(b.title, b.text) in review.digests()
    assert learner.counts() == (2, 1)
    assert [(e.title, e.text, e.kind) for e in learner.examples(True)] == [("测试用 pytest", "测试用 .venv 里的 pytest 跑。", "fact"), ("提交信息用中文", "这个项目的提交信息用中文写。", "convention")], "what the user kept in the end is what is learned"
    status = review.status(NOW + DAY)
    assert (status.last, status.skipped, status.due) == (NOW + DAY, 0, False)
    assert review.history() == [{"at": NOW + DAY, "kept": 2, "dropped": 1, "auto_kept": 0, "auto_dropped": 0, "labels": 3}]


def test_an_edit_can_give_a_note_to_its_project_or_make_it_global(tmp_path):
    store, review, learner = kit(tmp_path)
    here = write(store, review, "全局的一条", "原来是全局的。", project=project_path(PROJECT))
    nowhere = write(store, review, "没有项目的一条", "这个对话没有项目。")
    there = write(store, review, "项目的一条", "原来是项目的。", scope="project")
    assert (review.get(here.id).project, review.get(nowhere.id).project, review.get(there.id).project) == (project_path(PROJECT), "", project_path(PROJECT))
    review.apply([here.id, there.id], [], {here.id: {"scope": "project"}, there.id: {"scope": "global"}}, store=store, learner=learner, now=NOW)
    assert (store.read(here.id).scope, store.read(here.id).project) == ("project", project_path(PROJECT)), "to the project of its conversation"
    assert (store.read(there.id).scope, store.read(there.id).project) == ("global", "")
    with pytest.raises(MemoryRejected) as caught:
        review.apply([nowhere.id], [], {nowhere.id: {"scope": "project"}}, store=store, learner=learner, now=NOW)
    assert caught.value.code == "bad_scope", "a note from a conversation without a project has no project to go to"


def test_a_refused_edit_changes_nothing_in_the_review(tmp_path):
    store, review, learner = kit(tmp_path)
    a = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。")
    b = write(store, review, "临时调试输出", "在 loop.py 里加了一行 print。")
    key = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    with pytest.raises(MemoryRejected) as caught:
        review.apply([a.id], [b.id], {a.id: {"text": f"密钥是 {key}"}}, store=store, learner=learner, now=NOW + DAY)
    assert caught.value.code == "secret"
    assert review.get(a.id).state == review.get(b.id).state == PENDING and learner.labels == 0 and review.status(NOW).last == 0
    assert store.read(b.id).active, "nothing was dropped either"
    done = review.apply([a.id], [b.id], store=store, learner=learner, now=NOW + DAY)
    assert (done["kept"], done["dropped"]) == (1, 1), "and it can all be done again"


def test_what_is_not_waiting_or_named_twice_is_passed_over(tmp_path):
    store, review, learner = kit(tmp_path)
    a = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。")
    b = write(store, review, "测试命令", "测试用 pytest 跑。")
    done = review.apply(["m-nothing", a.id, a.id], [a.id, "m-else"], store=store, learner=learner, now=NOW)
    assert (done["kept"], done["dropped"], done["labels"], done["pending"]) == (0, 0, 0, 2), "kept and dropped at once: it waits"
    assert review.apply([b.id, b.id], [], store=store, learner=learner, now=NOW)["kept"] == 1
    again = review.apply([b.id], [b.id], store=store, learner=learner, now=NOW)
    assert (again["kept"], again["dropped"], again["labels"]) == (0, 0, 1) and review.get(b.id).state == KEPT, "decided once"
    assert review.apply([], [b.id], store=store, learner=learner, now=NOW)["dropped"] == 0 and store.read(b.id).active


def test_a_named_note_forgotten_meanwhile_is_the_users_drop(tmp_path):
    store, review, learner = kit(tmp_path)
    a = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。")
    store.forget(a.id, by="user")
    done = review.apply([a.id], [], store=store, learner=learner, now=NOW)
    assert (done["kept"], done["dropped"], done["labels"]) == (0, 1, 1)
    assert (review.get(a.id).state, review.get(a.id).by) == (DROPPED, "user") and learner.counts() == (0, 1)


def test_after_a_review_the_learner_tidies_what_it_is_sure_of(tmp_path):
    store, review, learner = kit(tmp_path)
    teach(learner)
    liked = write(store, review, "alpha beta", "gamma delta notes", tags=["keepme"], llm=0.6)
    disliked = write(store, review, "omega sigma", "theta kappa notes", kind="fact", scope="project", engine="devin", tags=["dropme"], llm=0.6)
    unsure = write(store, review, "部署命令", "部署用 make ship-it。", kind="pitfall", engine="chatgpt", llm=0.6)
    gone = write(store, review, "omega sigma two", "theta kappa again", kind="fact", scope="project", engine="devin", tags=["dropme"], llm=0.6)
    store.forget(gone.id, by="user")
    labels = learner.labels
    assert review.tidy(store, learner, NOW + DAY) == {"autoKept": 1, "autoDropped": 1}
    assert learner.labels == labels, "the learner's guesses are not lessons"
    assert (review.get(liked.id).state, review.get(liked.id).by, review.get(liked.id).decided_at) == (KEPT, "auto", NOW + DAY)
    assert store.read(liked.id).active
    assert (review.get(disliked.id).state, review.get(disliked.id).by) == (DROPPED, "auto")
    forgotten = store.read(disliked.id)
    assert forgotten.status == "forgotten" and forgotten.editor == APP and digest_of(disliked.title, disliked.text) in review.digests()
    left = review.get(unsure.id)
    assert left.state == PENDING and left.learned == learner.score(left) and left.score == combined(0.6, left.learned, True)
    assert review.get(gone.id).state == PENDING, "a note the user forgot is the user's drop, settled by the next batch"


def test_tidying_goes_by_the_words_a_note_has_now(tmp_path):
    store, review, learner = kit(tmp_path)
    plain = {"kind": "fact", "scope": "global", "engine": "codex"}
    learner.learn_all(
        [({"title": "alpha", "text": "lean", **plain}, True)] * 25 + [({"title": "alpha", "text": "drift", **plain}, True)] * 5
        + [({"title": "omega", "text": "drift", **plain}, False)] * 25 + [({"title": "omega", "text": "lean", **plain}, False)] * 5
    )
    disliked = write(store, review, "omega", "first notes", kind="fact", llm=0.6)
    liked = write(store, review, "alpha", "second notes", kind="fact", llm=0.6)
    assert review.get(disliked.id) and learner.score(review.get(disliked.id)) < 0.15 and learner.score(review.get(liked.id)) >= 0.9
    store.update(disliked.id, by="user", title="又一条", text="drift")
    store.update(liked.id, by="user", title="再一条", text="lean")
    now_disliked, now_liked = learner.score(store.read(disliked.id)), learner.score(store.read(liked.id))
    assert 0.15 < now_disliked < 0.5 < now_liked < 0.9, (now_disliked, now_liked)
    assert review.tidy(store, learner, NOW) == {"autoKept": 0, "autoDropped": 0}, "changed on the memory page: no longer sure"
    assert review.get(disliked.id).state == review.get(liked.id).state == PENDING and store.read(disliked.id).active


def test_a_learner_that_knows_little_tidies_nothing(tmp_path):
    store, review, learner = kit(tmp_path)
    learner.learn_all([({"title": "omega sigma", "text": "theta kappa", "kind": "fact"}, False)] * (MIN_LABELS - 1))
    disliked = write(store, review, "omega sigma", "theta kappa notes", kind="fact")
    assert review.tidy(store, learner, NOW) == {"autoKept": 0, "autoDropped": 0}
    assert review.get(disliked.id).state == PENDING and review.get(disliked.id).learned == 0.5


def test_a_review_ends_with_the_learner_tidying_and_the_history_says_so(tmp_path):
    store, review, learner = kit(tmp_path)
    teach(learner)
    kept = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。", llm=0.6)
    disliked = write(store, review, "omega sigma", "theta kappa notes", kind="fact", scope="project", engine="devin", tags=["dropme"], llm=0.6)
    done = review.apply([kept.id], [], store=store, learner=learner, now=NOW + DAY)
    assert (done["kept"], done["autoDropped"], done["pending"], done["labels"]) == (1, 1, 0, 61)
    assert done["next"] == 0, "nothing waits"
    assert review.history()[-1] == {"at": NOW + DAY, "kept": 1, "dropped": 0, "auto_kept": 0, "auto_dropped": 1, "labels": 61}
    assert not store.read(disliked.id).active


def test_the_history_keeps_half_a_year_and_the_words_five_hundred(tmp_path):
    store, review, learner = kit(tmp_path)
    for n in range(MAX_HISTORY + 4):
        review.apply([], [], store=store, learner=learner, now=NOW + n * DAY)
    history = review.history()
    assert len(history) == MAX_HISTORY and (history[0]["at"], history[-1]["at"]) == (NOW + 4 * DAY, NOW + (MAX_HISTORY + 3) * DAY)
    review.remember(f"d{n}" for n in range(MAX_DIGESTS + 20))
    review.remember(["d600", ""])
    digests = review.digests()
    assert len(digests) == MAX_DIGESTS and {"d21", "d519", "d600"} <= digests and not {"d0", "d20", ""} & digests, "the oldest go"


def test_decided_notes_leave_the_file_after_a_while(tmp_path):
    store, review, learner = kit(tmp_path)
    a = write(store, review, "提交信息用中文", "这个项目的提交信息用中文写。")
    b = write(store, review, "测试命令", "测试用 pytest 跑。")
    review.apply([a.id], [], store=store, learner=learner, now=NOW)
    write(store, review, "第三条", "第三条的内容。", at=NOW + KEEP_DECIDED_DAYS * DAY)
    assert review.get(a.id) is not None
    write(store, review, "第四条", "第四条的内容。", at=NOW + KEEP_DECIDED_DAYS * DAY + 1)
    assert review.get(a.id) is None and review.get(b.id) is not None, "a waiting note stays however old"


# ----------------------------------------------------------------------------- from the suggestions of before


def test_waiting_suggestions_move_into_the_memory_once(tmp_path):
    root = tmp_path / "memory"
    store, review, learner, proposals = MemoryStore(root), ReviewStore(root), Learner(root), ProposalStore(root)
    first = make(title="提交信息用中文", text="这个项目的提交信息用中文写。", scope="project", project=PROJECT, kind="convention", tags=["提交"], engine="chatgpt", conversation="对话一", row="cg:1", why="用户说过", now=NOW)
    second = make(title="回答用中文", text="用户希望回答都用中文。", scope="global", project="", kind="preference", tags=[], engine="codex", conversation="对话二", now=NOW + 5)
    refused = make(title="临时调试", text="加了一行 print。", scope="global", project="", kind="fact", tags=[], engine="codex", conversation="c", now=NOW)
    proposals.add([first, second, refused])
    proposals.dismiss(refused.id)
    assert migrate_proposals(proposals, store, review, learner, now=NOW + DAY) == 2
    assert proposals.pending() == [] and proposals.dismissed() == [refused.digest], "the moved ones are not taken for refused"
    notes = {e.title: e for e in store.entries()}
    assert set(notes) == {"提交信息用中文", "回答用中文"} and all(e.source == APP for e in notes.values())
    assert (notes["提交信息用中文"].scope, notes["提交信息用中文"].project, notes["提交信息用中文"].tags) == ("project", project_path(PROJECT), ("提交",))
    items = {item.title: item for item in review.pending()}
    one = items["提交信息用中文"]
    assert (one.id, one.engine, one.conversation, one.row, one.why, one.score, one.llm, one.learned, one.at) == (notes["提交信息用中文"].id, "chatgpt", "对话一", "cg:1", "用户说过", 0.5, 0.5, 0.5, NOW)
    assert items["回答用中文"].at == NOW + 5
    assert refused.digest in review.digests(), "what the user refused before stays refused"
    assert migrate_proposals(proposals, store, review, learner, now=NOW + 2 * DAY) == 0
    assert len(store.entries()) == 2 and review.count_pending() == 2


def test_a_move_that_stopped_half_way_is_finished_and_a_duplicate_of_the_users_goes(tmp_path):
    root = tmp_path / "memory"
    store, review, learner, proposals = MemoryStore(root), ReviewStore(root), Learner(root), ProposalStore(root)
    half = make(title="提交信息用中文", text="这个项目的提交信息用中文写。", scope="global", project="", kind="convention", tags=[], engine="chatgpt", conversation="c", now=NOW)
    mine = make(title="测试命令", text="测试用 pytest 跑。", scope="global", project="", kind="convention", tags=[], engine="chatgpt", conversation="c", now=NOW)
    proposals.add([half, mine])
    written = store.create(title=half.title, text=half.text, scope="global", kind="convention", by=APP)
    store.create(title="测试命令", text="自己写的。", scope="global", kind="fact", by="user")
    assert migrate_proposals(proposals, store, review, learner, now=NOW) == 1
    assert [item.id for item in review.pending()] == [written.id] and len(store.entries()) == 2
    assert proposals.pending() == [] and proposals.dismissed() == []


def test_a_full_memory_leaves_the_suggestion_for_another_time(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    store, review, learner, proposals = MemoryStore(root), ReviewStore(root), Learner(root), ProposalStore(root)
    proposals.add([make(title="回答用中文", text="用户希望回答都用中文。", scope="global", project="", kind="preference", tags=[], engine="codex", conversation="c")])
    store.create(title="已有的一条", text="已经有了。", scope="global", kind="fact", by="user")
    monkeypatch.setattr(store_module, "MAX_ACTIVE", 1)
    assert migrate_proposals(proposals, store, review, learner) == 0 and proposals.count() == 1 and review.count_pending() == 0
    monkeypatch.setattr(store_module, "MAX_ACTIVE", 500)
    assert migrate_proposals(proposals, store, review, learner) == 1 and proposals.count() == 0


# ----------------------------------------------------------------------------- the page's side (MemoryBridge)


def bridge_for(tmp_path, **more):
    launch = MemoryLaunch(tmp_path / "memory", python="py.exe")
    clock = more.pop("clock", None) or Clock()
    return MemoryBridge(launch, row_id=lambda engine, native: f"{engine}:{native}", clock=clock, **more), clock


def test_the_bridge_owns_one_review_and_one_learner_with_the_launch(tmp_path):
    bridge, _ = bridge_for(tmp_path)
    assert bridge.review is bridge.launch.review and bridge.learner is bridge.launch.learner
    assert bridge.review.path == tmp_path / "memory" / "review.json" and bridge.learner.path == tmp_path / "memory" / "learned.json"
    assert bridge.review_mode is False


def test_the_review_state_for_the_page(tmp_path):
    bridge, clock = bridge_for(tmp_path)
    assert bridge.review_state() == {"due": False, "pending": 0, "last": 0, "next": 0, "labels": 0, "size": REVIEW_SIZE, "skipped": 0}
    for n in range(DUE_MIN):
        write(bridge.store, bridge.review, f"笔记 {n}", f"内容 {n}", at=NOW)
    bridge.review.skip(2)
    bridge.learner.learn({"title": "t", "text": "x"}, True)
    assert bridge.review_state() == {"due": False, "pending": DUE_MIN, "last": 0, "next": int((NOW + WEEK) * 1000), "labels": 1, "size": REVIEW_SIZE, "skipped": 2}
    clock.now = NOW + WEEK
    assert bridge.review_state()["due"] is True and bridge.review_state({}) == bridge.review_state(), "callable like the other calls of the page"


def test_in_review_mode_the_badge_counts_a_review_only_when_it_is_due(tmp_path):
    legacy, clock = bridge_for(tmp_path)
    legacy.proposals.add([make(title="一条建议", text="建议的内容。", scope="global", project="", kind="fact", tags=[], engine="codex", conversation="c")])
    for n in range(DUE_MIN):
        write(legacy.store, legacy.review, f"笔记 {n}", f"内容 {n}", at=NOW)
    assert legacy.state()["pending"] == 1, "the old meaning: suggestions waiting"
    reviewing, _ = bridge_for(tmp_path, review_mode=True, clock=clock)
    assert reviewing.state()["pending"] == 0, "not due yet"
    clock.now = NOW + WEEK
    assert reviewing.state()["pending"] == DUE_MIN and set(reviewing.state()) == {"latest", "unseen", "seen", "pending"}


def test_what_the_application_wrote_is_its_own_and_not_news(tmp_path):
    bridge, _ = bridge_for(tmp_path)
    user_note = bridge.store.create(title="我的一条", text="我自己写的。", scope="global", kind="fact", by="user")
    agent_note = bridge.store.create(title="代理的一条", text="代理写的。", scope="global", kind="fact", by=agent("codex", "r1"))
    app_note = write(bridge.store, bridge.review, "应用的一条", "应用自己写的。")
    bridge.store.forget(app_note.id, by=APP)
    state = bridge.state()
    assert state["unseen"] == 1 and state["latest"] == 4, "the agent's note is news; the application's is not, but it is a change"
    assert [c["id"] for c in bridge.changes({})["changes"]] == [agent_note.id]
    listed = {e["title"]: e for e in bridge.list({})["entries"]} | {e["title"]: e for e in bridge.list({"status": "all"})["entries"]}
    assert listed["应用的一条"]["source"] == {"kind": "app"} and listed["应用的一条"]["editor"] == {"kind": "app"}
    assert [e["id"] for e in bridge.list({"engine": "app", "status": "all"})["entries"]] == [app_note.id]
    assert [e["id"] for e in bridge.list({"engine": "user", "status": "all"})["entries"]] == [user_note.id], "the application is not the user"
    assert [e["id"] for e in bridge.list({"engine": "app", "query": "应用自己", "status": "all"})["entries"]] == [app_note.id]
    assert [e["id"] for e in bridge.list({"engine": "user", "query": "写的", "status": "all"})["entries"]] == [user_note.id]


def test_a_review_batch_for_the_page(tmp_path):
    bridge, clock = bridge_for(tmp_path)
    teach(bridge.learner)
    project = write(bridge.store, bridge.review, "omega sigma", "theta kappa notes", kind="fact", scope="project", engine="devin", tags=["dropme"], llm=0.6, at=NOW)
    plain = write(bridge.store, bridge.review, "alpha beta", "gamma delta notes", tags=["keepme"], llm=0.6, at=NOW + 1)
    clock.now = NOW + WEEK
    batch = bridge.review_batch({})
    assert [item["id"] for item in batch["items"]] == [project.id, plain.id] and batch["total"] == 2
    low, high = batch["items"]
    assert set(low) == {"id", "title", "text", "kind", "scope", "project", "projectName", "tags", "engine", "conversation", "row", "why", "score", "explain", "at", "suggestKeep"}
    assert (low["title"], low["text"], low["kind"], low["scope"], low["project"], low["projectName"], low["tags"]) == ("omega sigma", "theta kappa notes", "fact", "project", project_path(PROJECT), "demo", ["dropme"])
    assert (low["engine"], low["conversation"], low["row"], low["why"], low["at"]) == ("devin", "测试对话", "cg:1", "用户明确说了", int(NOW * 1000))
    assert low["score"] == round(low["score"], 2) and low["score"] < 0.5 and low["suggestKeep"] is False
    assert high["score"] > 0.5 and high["suggestKeep"] is True
    assert low["explain"] and all(isinstance(reason, str) and ":" not in reason for reason in low["explain"])
    assert {key: batch[key] for key in ("due", "pending", "labels", "size")} == {"due": False, "pending": 2, "labels": 60, "size": REVIEW_SIZE}
    assert [item["id"] for item in bridge.review_batch({"size": 1})["items"]] == [project.id]
    for size in (0, REVIEW_SIZE + 1, "3", True, 2.0):
        with pytest.raises(MemoryRejected) as caught:
            bridge.review_batch({"size": size})
        assert caught.value.code == "bad_request", size


def test_a_review_applied_from_the_page(tmp_path):
    bridge, clock = bridge_for(tmp_path, review_mode=True)
    a = write(bridge.store, bridge.review, "提交信息用中文", "这个项目的提交信息用中文写。")
    b = write(bridge.store, bridge.review, "临时调试输出", "在 loop.py 里加了一行 print。")
    c = write(bridge.store, bridge.review, "还没看", "这一条留着。")
    clock.now = NOW + DAY
    done = bridge.review_apply({"keep": [a.id], "drop": [b.id], "edits": {a.id: {"title": "提交信息一律用中文", "tags": ["提交"]}}})
    assert {key: done[key] for key in ("kept", "dropped", "autoKept", "autoDropped", "labels", "left", "next")} == {
        "kept": 1, "dropped": 1, "autoKept": 0, "autoDropped": 0, "labels": 2, "left": 1, "next": int((NOW + DAY + WEEK) * 1000)
    }
    assert set(done) == {"kept", "dropped", "autoKept", "autoDropped", "labels", "left", "next", "latest", "unseen", "seen", "pending"}
    assert done["pending"] == 0, "the badge: no review is due right after one"
    assert bridge.store.read(a.id).title == "提交信息一律用中文" and not bridge.store.read(b.id).active and bridge.review.get(c.id).state == PENDING
    assert bridge.review_apply({})["kept"] == 0, "nothing named: nothing done"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"keep": "m-1"}, "bad_request"),
        ({"keep": ["m-1", 2]}, "bad_request"),
        ({"drop": [""]}, "bad_request"),
        ({"drop": {"m-1": True}}, "bad_request"),
        ({"keep": ["m-1"], "drop": ["m-1"]}, "bad_request"),
        ({"edits": ["m-1"]}, "bad_request"),
        ({"edits": {"m-1": "new title"}}, "bad_request"),
        ({"edits": {"m-1": {"id": "m-2"}}}, "bad_request"),
        ({"edits": {"m-1": {"title": 5}}}, "bad_request"),
        ({"edits": {"m-1": {"tags": "a, b"}}}, "bad_request"),
        ({"edits": {"m-1": {"tags": ["a", 1]}}}, "bad_request"),
        ({"edits": {"m-1": {"kind": "weird"}}}, "bad_kind"),
        ({"edits": {"m-1": {"scope": "everywhere"}}}, "bad_scope"),
    ],
)
def test_a_review_from_the_page_that_is_not_in_shape_is_refused_whole(tmp_path, params, code):
    bridge, _ = bridge_for(tmp_path)
    a = write(bridge.store, bridge.review, "提交信息用中文", "这个项目的提交信息用中文写。")
    with pytest.raises(MemoryRejected) as caught:
        bridge.review_apply({"keep": [a.id], **params} if "keep" not in params else params)
    assert caught.value.code == code
    assert bridge.review.get(a.id).state == PENDING and bridge.learner.labels == 0
    assert explain(caught.value) == MESSAGES[code] and "记忆没有改动" in MESSAGES["bad_request"]
