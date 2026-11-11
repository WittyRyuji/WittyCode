"""What the user keeps, learned from the user's own verdicts on the application's notes (``core/memory/learn``)."""

from __future__ import annotations

import json
import math
import threading

import pytest

from desktop.core.memory import learn as learn_module
from desktop.core.memory.learn import EVIDENCE, MIN_LABELS, PRIOR_WEIGHT, Learner, Sample, features, sample_of, terms
from desktop.core.memory.proposals import make
from desktop.core.memory.store import MemoryStore, agent

PROJECT = "D:\\work\\demo"


def note(title, text, *, kind="convention", scope="project", engine="codex", tags=(), project=PROJECT):
    return {"title": title, "text": text, "kind": kind, "scope": scope, "engine": engine, "tags": list(tags), "project": project}


KEPT = [
    note("提交信息用中文", "这个项目的提交信息用中文写。"),
    note("e2e 要连跑 20 次", "改动 e2e 相关代码后要整文件连跑 20 次才算过。"),
    note("测试用 pytest", "这个项目的测试用 pytest 跑。"),
    note("PowerShell 不支持 &&", "Windows PowerShell 5.1 不支持 &&，命令之间用分号。", kind="pitfall"),
]
DROPPED = [
    note("修了一个拼写错误", "这次把 README 里的一个拼写错误改掉了。", kind="fact"),
    note("临时调试输出", "在 loop.py 里临时加了一行 print 调试输出。", kind="fact"),
    note("今天的任务完成了", "按要求改完了按钮的颜色，任务完成。", kind="fact"),
]


def taught(root):
    learner = Learner(root, clock=lambda: 1000.0)
    learner.learn_all([(c, True) for c in KEPT] + [(c, False) for c in DROPPED])
    return learner


# ----------------------------------------------------------------------------- what a note is made of


def test_latin_words_count_from_two_letters_without_numbers_or_everyday_words():
    assert terms("Use the e2e suite 20 times, a B ok") == {"use", "e2e", "suite", "times", "ok"}
    assert terms("ＰＹＴＥＳＴ　Pytest") == {"pytest"}, "full-width letters and capitals are the same word"


def test_chinese_is_read_in_pairs_of_characters():
    assert terms("提交信息用中文") == {"提交", "交信", "信息", "息用", "用中", "中文"}
    assert terms("改") == set() and terms("的") == set(), "one character says too little"
    assert terms("我的项目") == {"项目"}, "的 only holds the sentence together: no pair spans it"
    assert terms("可以吗") == set() and "这个" not in terms("这个项目"), "everyday pairs say nothing"
    assert terms("用 pnpm 安装") == {"pnpm", "安装"}


def test_kind_scope_engine_and_tags_are_features_too():
    found = features(note("Use tabs", "Always use tabs", kind="preference", scope="global", engine=" ChatGPT ", tags=["Style ", "  Code  Style", ""]))
    assert found == {"use", "tabs", "always", "kind:preference", "scope:global", "engine:chatgpt", "tag:style", "tag:code style"}
    assert features(note("Use tabs", "x", kind="weird", scope="weird", engine="")) == {"use", "tabs"}, "what is not a kind or a scope is left out"


def test_a_note_is_read_from_a_mapping_an_entry_or_a_suggestion(tmp_path):
    entry = MemoryStore(tmp_path / "m").create(title="测试用 pytest", text="这个项目的测试用 pytest 跑。", scope="global", kind="convention", tags=["测试"], by=agent("codex"))
    proposal = make(title=entry.title, text=entry.text, scope="global", project="", kind="convention", tags=["测试"], engine="codex", conversation="c")
    mapping = {"title": entry.title, "text": entry.text, "scope": "global", "kind": "convention", "tags": ["测试"], "engine": "codex"}
    assert features(entry) == features(proposal) == features(mapping) == features(sample_of(mapping))
    assert "engine:codex" in features(entry), "an entry's engine is its author's"
    assert sample_of(object()) == Sample("", "") and sample_of({"tags": "not a list"}).tags == ()
    assert sample_of(mapping).project == "" and sample_of(note("t", "x")).project == PROJECT


# ----------------------------------------------------------------------------- the score


def test_with_nothing_learned_every_note_is_an_even_guess(tmp_path):
    learner = Learner(tmp_path)
    for candidate in (*KEPT, *DROPPED, note("", ""), {}):
        assert learner.score(candidate) == 0.5
    assert learner.labels == 0 and learner.ready is False and learner.counts() == (0, 0)
    assert learner.explain(KEPT[0]) == [] and learner.examples(True) == [] and learner.examples(False) == []
    assert not (tmp_path / "learned.json").exists(), "reading writes nothing"


def test_the_verdicts_move_the_score_the_way_the_user_went(tmp_path):
    learner = taught(tmp_path)
    like_kept = note("提交信息要用中文", "提交信息用中文写，标题简短。")
    like_dropped = note("又修了一个拼写错误", "把注释里的一个拼写错误改掉了。", kind="fact")
    assert learner.score(like_kept) > 0.65 > 0.35 > learner.score(like_dropped)
    for candidate in KEPT:
        assert learner.score(candidate) > 0.5, candidate["title"]
    for candidate in DROPPED:
        assert learner.score(candidate) < 0.5, candidate["title"]


def test_the_score_is_naive_bayes_pulled_towards_one_half(tmp_path):
    learner = Learner(tmp_path)
    kept = note("Use tabs", "always tabs", kind="preference", scope="global")
    learner.learn(kept, True)
    # six features (use, always, tabs, kind, scope, engine), each in the one kept note and in none dropped; the odds of
    # the verdicts are 2 : 1
    odds = math.log(2 / 1) + 6 * (math.log(2 / 3) - math.log(1 / 2))
    expected = (1 / (1 + math.exp(-odds)) + PRIOR_WEIGHT * 0.5) / (1 + PRIOR_WEIGHT)
    assert learner.score(kept) == pytest.approx(expected)
    assert 0.5 < learner.score(kept) < (1 + PRIOR_WEIGHT * 0.5) / (1 + PRIOR_WEIGHT), "one verdict is far from sure"
    unseen = {"title": "", "text": "nothing like it"}
    assert learner.score(unseen) == pytest.approx((1 / (1 + 1 / 2) + PRIOR_WEIGHT * 0.5) / (1 + PRIOR_WEIGHT)), "only the odds of the verdicts"


def test_many_verdicts_make_the_score_sure_but_never_certain(tmp_path):
    learner = Learner(tmp_path)
    keep, drop = note("alpha beta", "gamma", kind="preference"), note("delta epsilon", "zeta", kind="fact")
    learner.learn_all([(keep, True)] * 100 + [(drop, False)] * 100)
    labels = 200
    assert learner.score(keep) == pytest.approx((labels + PRIOR_WEIGHT * 0.5) / (labels + PRIOR_WEIGHT), abs=1e-6)
    assert learner.score(drop) == pytest.approx(PRIOR_WEIGHT * 0.5 / (labels + PRIOR_WEIGHT), abs=1e-6)
    assert 0 < learner.score(drop) < learner.score(keep) < 1


def test_a_kept_chinese_note_lifts_a_similar_chinese_one(tmp_path):
    learner = Learner(tmp_path)
    learner.learn(note("提交信息用中文", "这个项目的提交信息用中文写。"), True)
    learner.learn(note("按钮颜色", "按钮改成了蓝色。"), False)
    similar = note("提交说明用中文", "提交信息一律用中文。")
    unrelated = note("开发端口", "开发服务器监听八千端口。")
    assert learner.score(similar) > 0.55 and learner.score(similar) > learner.score(unrelated) + 0.05


def test_a_long_note_is_not_surer_than_a_short_one(tmp_path):
    learner = Learner(tmp_path)
    words = [f"word{n}" for n in range(40)]
    learner.learn({"title": "", "text": " ".join(words)}, False)
    learner.learn({"title": "", "text": "something else"}, True)
    short, long = {"text": " ".join(words[:EVIDENCE])}, {"text": " ".join(words)}
    assert learner.score(short) < 0.5
    assert learner.score(long) == pytest.approx(learner.score(short)), "past EVIDENCE features the average counts"
    assert learner.score({"text": " ".join(words[:3])}) > learner.score(short), "up to EVIDENCE, more evidence is surer"


def test_it_is_ready_from_min_labels_verdicts(tmp_path):
    learner = Learner(tmp_path)
    learner.learn_all([(note(f"n{i}", "x"), i % 2 == 0) for i in range(MIN_LABELS - 1)])
    assert learner.ready is False and learner.labels == MIN_LABELS - 1
    learner.learn(note("last", "x"), False)
    assert learner.ready is True and learner.counts() == (MIN_LABELS // 2, MIN_LABELS // 2)


# ----------------------------------------------------------------------------- why


def test_the_reasons_are_words_the_user_reads(tmp_path):
    learner = taught(tmp_path)
    reasons = learner.explain(note("提交信息要用中文", "提交信息用中文写。"))
    assert reasons[:2] == ["提交信息用中文写", "约定"] and len(reasons) == 3
    assert "交信" not in reasons and "提交信息" not in reasons, "a run of characters, not its pairs, and not again inside a longer one"
    assert not any(":" in reason for reason in reasons)
    assert learner.explain(note("提交信息要用中文", "提交信息用中文写。"), top=1) == ["提交信息用中文写"]
    assert learner.explain(note("提交信息要用中文", "x"), top=0) == []
    against = learner.explain(note("又一个拼写错误", "拼写错误改掉了。", kind="fact"))
    assert against[0] == "拼写错误改掉" and "事实" in against, "a low score is explained by what pulled it down"


def test_kind_scope_and_engine_read_as_words(tmp_path):
    learner = Learner(tmp_path)
    for n in range(3):
        learner.learn(note(f"k{n}", f"alpha{n}", kind="convention", scope="global", engine="codex"), True)
        learner.learn(note(f"d{n}", f"beta{n}", kind="fact", scope="project", engine="devin"), False)
    assert set(learner.explain(note("x", "y", kind="convention", scope="global", engine="codex"))) == {"约定", "全局", "Codex"}
    assert set(learner.explain(note("x", "y", kind="fact", scope="project", engine="devin"))) == {"事实", "项目", "Devin"}
    learner.learn(note("t", "z", kind="pitfall", engine="chatgpt", tags=["部署"]), True)
    assert set(learner.explain(note("x", "y", kind="pitfall", scope="", engine="chatgpt", tags=["部署"]))) == {"坑", "ChatGPT Plan", "部署"}


# ----------------------------------------------------------------------------- examples for the model


def test_examples_are_the_newest_first_and_never_twice_the_same(tmp_path):
    ticks = iter(range(1000, 2000))
    learner = Learner(tmp_path, clock=lambda: float(next(ticks)))
    for title, text in (("提交信息用中文", "一"), ("测试用 pytest", "二"), ("提交信息用中文。", "三"), ("Step 1 build", "四"), ("Step 2 build", "五")):
        learner.learn(note(title, text), True)
    learner.learn(note("临时调试", "六"), False)
    assert [e.title for e in learner.examples(True)] == ["Step 2 build", "Step 1 build", "提交信息用中文。", "测试用 pytest"]
    assert [e.title for e in learner.examples(True, 2)] == ["Step 2 build", "Step 1 build"]
    assert [e.title for e in learner.examples(False)] == ["临时调试"] and learner.examples(True, 0) == []
    first = learner.examples(True)[0]
    assert (first.text, first.kind, first.scope, first.project, first.at) == ("五", "convention", "project", PROJECT, 1004.0)


def test_examples_from_another_project_are_not_shown(tmp_path):
    learner = Learner(tmp_path)
    learner.learn(note("项目甲的约定", "甲", project="D:\\a"), True)
    learner.learn(note("全局的偏好", "乙", scope="global", project=""), True)
    learner.learn(note("项目乙的约定", "丙", project="D:\\b"), True)
    assert [e.title for e in learner.examples(True, project="d:\\A")] == ["全局的偏好", "项目甲的约定"]
    assert [e.title for e in learner.examples(True, project="")] == ["全局的偏好"], "no project: the global ones"
    assert len(learner.examples(True)) == 3, "None: all of them"


def test_only_the_newest_examples_are_kept_and_their_text_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(learn_module, "MAX_EXAMPLES", 3)
    learner = Learner(tmp_path)
    for n in range(5):
        learner.learn(note(f"第 {n} 条", "长" * 500), True)
    data = json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))
    assert [e["title"] for e in data["examples"]["kept"]] == ["第 2 条", "第 3 条", "第 4 条"]
    assert all(len(e["text"]) == learn_module.EXAMPLE_CHARS for e in data["examples"]["kept"])
    assert learner.counts() == (5, 0), "the counts are not cut"


# ----------------------------------------------------------------------------- learning and forgetting


def test_what_was_learned_survives_a_restart(tmp_path):
    first = taught(tmp_path)
    again = Learner(tmp_path)
    for candidate in (*KEPT, *DROPPED):
        assert again.score(candidate) == first.score(candidate)
    assert again.counts() == first.counts() == (len(KEPT), len(DROPPED))
    assert again.examples(True) == first.examples(True) and again.examples(False) == first.examples(False)


def test_two_learners_of_one_file_see_each_others_lessons(tmp_path):
    a, b = Learner(tmp_path), Learner(tmp_path)
    assert b.labels == 0
    a.learn(KEPT[0], True)
    assert b.labels == 1 and b.score(KEPT[0]) == a.score(KEPT[0])
    b.learn(DROPPED[0], False)
    assert a.counts() == (1, 1)


def test_learners_of_one_file_take_turns(tmp_path):
    a, b = Learner(tmp_path), Learner(tmp_path / ".")
    assert a._lock is b._lock and Learner(tmp_path / "other")._lock is not a._lock

    def teach(learner, side):
        for n in range(15):
            learner.learn(note(f"{side} {n}", "x"), True)

    threads = [threading.Thread(target=teach, args=(learner, side)) for learner, side in ((a, "a"), (b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert Learner(tmp_path).counts() == (30, 0), "no lesson is lost"


def test_several_verdicts_are_learned_in_one_go(tmp_path):
    learner = Learner(tmp_path)
    assert learner.learn_all([]) == 0 and not (tmp_path / "learned.json").exists()
    assert learner.learn_all([(KEPT[0], 1), (DROPPED[0], 0), (KEPT[1], True)]) == 3
    assert learner.counts() == (2, 1)


def test_a_verdict_can_be_taken_back(tmp_path):
    learner = taught(tmp_path)
    before = [learner.score(c) for c in (*KEPT, *DROPPED)]
    wrong = note("多余的一条", "这条是误判，提交信息。")
    learner.learn(wrong, False)
    assert [learner.score(c) for c in (*KEPT, *DROPPED)] != before
    assert learner.forget(wrong, False) is True
    assert [learner.score(c) for c in (*KEPT, *DROPPED)] == before and learner.counts() == (len(KEPT), len(DROPPED))
    assert learner.forget(wrong, False) is False, "once"
    assert learner.forget(KEPT[0], False) is False, "it was not dropped"
    assert learner.forget(note("从没学过", "x"), True) is False
    assert [e.title for e in learner.examples(False)] == [c["title"] for c in reversed(DROPPED)]
    assert Learner(tmp_path).counts() == (len(KEPT), len(DROPPED)), "written"


def test_a_lesson_that_could_not_be_written_is_not_half_learned(tmp_path, monkeypatch):
    learner = Learner(tmp_path)
    learner.learn(KEPT[0], True)

    def full(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(learn_module.os, "replace", full)
    with pytest.raises(OSError):
        learner.learn(DROPPED[0], False)
    monkeypatch.undo()
    assert learner.counts() == (1, 0) and learner.score(DROPPED[0]) == Learner(tmp_path).score(DROPPED[0]), "as the file has it"


def test_a_damaged_file_is_nothing_learned_and_is_written_over(tmp_path):
    path = tmp_path / "learned.json"
    for rubbish in ("{not json", "[1, 2]", "null", json.dumps({"kept": -3, "dropped": "x", "terms": {"a": "b", "": [1, 1], "提交": [5, 0, 1]}, "examples": {"kept": [1, {"title": 2}], "dropped": "no"}})):
        path.write_text(rubbish, encoding="utf-8")
        learner = Learner(tmp_path)
        assert learner.counts() == (0, 0) and learner.score(KEPT[0]) == 0.5 and learner.examples(True) == [], rubbish
    learner.learn(KEPT[0], True)
    assert Learner(tmp_path).counts() == (1, 0)
    path.write_text(json.dumps({"kept": 2, "dropped": 1, "seq": 1, "terms": {"提交": [9, 9, 7], "信息": [1, 0]}}), encoding="utf-8")
    learner = Learner(tmp_path)
    assert 0 < learner.score(note("提交信息", "")) < 1, "counts beyond the verdicts are held to them"
    learner.learn(KEPT[0], True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["terms"]["提交"][:2] == [3, 1] and data["seq"] == 8, "the newest verdict is newer than any feature"


def test_the_rarest_and_oldest_features_go_when_there_are_too_many(tmp_path, monkeypatch):
    monkeypatch.setattr(learn_module, "MAX_TERMS", 30)
    learner = Learner(tmp_path)
    common = note("common words", "alpha beta", kind="fact", scope="global")
    learner.learn_all([(common, True)] * 3)
    for n in range(20):
        learner.learn(note(f"w{n}", f"word{n}x extra{n}y", kind="fact", scope="global"), n % 2 == 0)
        assert len(json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))["terms"]) <= 30
    kept = set(json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))["terms"])
    assert {"alpha", "beta", "common", "words", "kind:fact", "scope:global", "engine:codex"} <= kept, "the common ones stay"
    assert {"w19", "word19x", "extra19y"} <= kept, "the newest stay"
    assert not {"w0", "word0x", "extra0y"} & kept, "the oldest of the rare ones go"
