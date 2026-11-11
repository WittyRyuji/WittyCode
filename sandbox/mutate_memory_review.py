"""Mutation checks for the notes the application writes on its own and the weekly review: the learner (``learn.py``),
the review (``review.py``), the curator's writing (``curator.py``), and their parts in the bridge, the launch, the store
and the suggestions.

    python sandbox/mutate_memory_review.py            run every mutant
    python sandbox/mutate_memory_review.py --check    only check that every pattern is found exactly once
    python sandbox/mutate_memory_review.py 3 17       run just these mutants

The mutants are made in a copy of the package and the tests in a temporary folder, never in the working tree: other
work in the same tree (and its tests) goes on undisturbed while this runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
MEM = Path("desktop") / "core" / "memory"
LEARN, REVIEW, CURATOR, BRIDGE, LAUNCH, STORE, PROPOSALS = (
    MEM / name for name in ("learn.py", "review.py", "curator.py", "bridge.py", "launch.py", "store.py", "proposals.py")
)

LEARNING = ["tests/test_memory_learn.py"]
REVIEWING = ["tests/test_memory_review.py"]
WRITING = ["tests/test_memory_curator_auto.py"]
TESTS = {
    LEARN: LEARNING + REVIEWING + WRITING,
    REVIEW: REVIEWING + WRITING,
    CURATOR: WRITING + ["tests/test_memory_curator.py"],
    BRIDGE: REVIEWING,
    LAUNCH: WRITING + REVIEWING + ["tests/test_memory_capture.py"],
    STORE: REVIEWING + ["tests/test_memory_store.py"],
    PROPOSALS: REVIEWING + ["tests/test_memory_curator.py"],
}

M = [
    # ---- what a note is made of
    ("learn: one letter is a word", LEARN, "if len(word) >= 2 and not word.isdigit()", "if len(word) >= 1 and not word.isdigit()"),
    ("learn: a number is a word", LEARN, " and not word.isdigit() and word not in STOP_WORDS}", " and word not in STOP_WORDS}"),
    ("learn: everyday words count", LEARN, " and word not in STOP_WORDS}", "}"),
    ("learn: everyday pairs count", LEARN, " if pair not in STOP_PAIRS)", ")"),
    ("learn: a pair spans the characters that join a sentence", LEARN, "        for part in _BREAK.split(run):\n            found.update(", "        for part in [run]:\n            found.update("),
    ("learn: full-width letters and capitals are other words", LEARN, "    flat = unicodedata.normalize(\"NFKC\", text).casefold()\n", "    flat = text\n"),
    ("learn: the kind is no feature", LEARN, "        found.add(f\"kind:{sample.kind}\")\n", "        pass\n"),
    ("learn: the scope is no feature", LEARN, "        found.add(f\"scope:{sample.scope}\")\n", "        pass\n"),
    ("learn: the engine is no feature", LEARN, "        found.add(f\"engine:{sample.engine.strip().casefold()}\")\n", "        pass\n"),
    ("learn: tags are no features", LEARN, "            found.add(f\"tag:{flat}\")\n", "            pass\n"),
    ("learn: an entry's engine is not its author's", LEARN, "        engine = source.get(\"engine\") if isinstance(source, Mapping) else \"\"\n", "        engine = \"\"\n"),
    # ---- the score
    ("learn: the kept counts are not smoothed", LEARN, "math.log((counts[0] + 1) / (kept + 2))", "math.log((counts[0] + 0.01) / (kept + 2))"),
    ("learn: the dropped counts are smoothed otherwise", LEARN, "math.log((counts[1] + 1) / (dropped + 2))", "math.log((counts[1] + 1) / (dropped + 1))"),
    ("learn: the odds of the verdicts are left out", LEARN, "    return math.log((kept + 1) / (dropped + 1)) + evidence, weights", "    return evidence, weights"),
    ("learn: few verdicts are believed", LEARN, "        return (labels * _sigmoid(odds) + PRIOR_WEIGHT * 0.5) / (labels + PRIOR_WEIGHT)", "        return _sigmoid(odds)"),
    ("learn: few verdicts pull towards nothing", LEARN, "_sigmoid(odds) + PRIOR_WEIGHT * 0.5)", "_sigmoid(odds) + 0)"),
    ("learn: a long note is surer", LEARN, "    if len(weights) > EVIDENCE:\n        evidence *= EVIDENCE / len(weights)\n", ""),
    ("learn: a short note counts as a long one", LEARN, "    if len(weights) > EVIDENCE:", "    if weights:"),
    ("learn: ready one verdict early", LEARN, "return self.labels >= MIN_LABELS", "return self.labels >= MIN_LABELS - 1"),
    # ---- learning
    ("learn: a verdict is counted the other way", LEARN, "                    column = 0 if kept else 1\n                    for term in features(sample):", "                    column = 1 if kept else 0\n                    for term in features(sample):"),
    ("learn: when a feature was last seen is not kept", LEARN, "                        counts[2] = model.seq\n", "                        pass\n"),
    ("learn: the newest examples go", LEARN, "                    del shelf[:-MAX_EXAMPLES]\n", "                    del shelf[MAX_EXAMPLES:]\n"),
    ("learn: an example's text is not cut", LEARN, "{\"title\": sample.title, \"text\": sample.text[:EXAMPLE_CHARS],", "{\"title\": sample.title, \"text\": sample.text,"),
    ("learn: the features have no limit", LEARN, "                _prune(model)\n", ""),
    ("learn: the commonest features go", LEARN, "key=lambda item: (item[1][0] + item[1][1], item[1][2], item[0])", "key=lambda item: (-(item[1][0] + item[1][1]), item[1][2], item[0])"),
    ("learn: the newest of the rare features go", LEARN, "key=lambda item: (item[1][0] + item[1][1], item[1][2], item[0])", "key=lambda item: (item[1][0] + item[1][1], -item[1][2], item[0])"),
    ("learn: counts beyond the verdicts are believed", LEARN, "kept, dropped = min(_whole(value[0]), model.kept), min(_whole(value[1]), model.dropped)", "kept, dropped = _whole(value[0]), _whole(value[1])"),
    ("learn: a failed write is half learned", LEARN, "            self._cache = self._stamp = None  # (the model in hand", "            pass  # (the model in hand"),
    ("learn: the file is read once", LEARN, "        if self._cache is None or stamp != self._stamp:", "        if self._cache is None:"),
    ("learn: each learner has a lock of its own", LEARN, "        self._lock = file_lock(self.path)\n", "        self._lock = threading.RLock()\n"),
    ("learn: one lock for every file", LEARN, "        return _LOCKS.setdefault(key, threading.RLock())\n", "        return _LOCKS.setdefault(\"\", threading.RLock())\n"),
    ("learn: a damaged file is believed", LEARN, "                except (OSError, ValueError, RecursionError):\n                    model = _Model()\n", "                except ZeroDivisionError:\n                    model = _Model()\n"),
    ("learn: taking back takes nothing back", LEARN, "                if counts is not None and counts[column]:\n                    counts[column] -= 1\n", ""),
    ("learn: a verdict never learned is taken back", LEARN, "            if index is None or not (model.kept if kept else model.dropped):\n                return False\n", "            if False:\n                return False\n"),
    # ---- examples and reasons
    ("learn: the oldest examples come first", LEARN, "        for raw in reversed(shelf):", "        for raw in shelf:"),
    ("learn: the same title is shown twice", LEARN, "            if any(_same_subject(title, other) for other in titles):\n                continue\n", ""),
    ("learn: another project's examples are shown", LEARN, "if project is not None and raw[\"scope\"] != \"global\" and not same_project(raw[\"project\"], project):", "if False:"),
    ("learn: the global examples are not shown in a project", LEARN, " and raw[\"scope\"] != \"global\" and not same_project(", " and not same_project("),
    ("learn: the reasons are always for keeping", LEARN, "        side = 1.0 if odds >= 0 else -1.0\n", "        side = 1.0\n"),
    ("learn: the reasons are pairs of characters", LEARN, "                    phrases.append((math.fsum(pool[pair] for pair in pairs), part[start : end + 1]))\n                    merged |= pairs\n", ""),
    ("learn: a run is given again inside a longer one", LEARN, " or (_CJK.fullmatch(label) and any(label in other for other in out))", ""),
    ("learn: the kind is not in words", LEARN, "        return _KIND_WORDS.get(rest, rest)\n", "        return rest\n"),
    ("learn: the scope is not in words", LEARN, "        return _SCOPE_WORDS.get(rest, rest)\n", "        return rest\n"),
    # ---- when a review is due
    ("review: fewer than DUE_MIN notes are a review", REVIEW, "        if count < DUE_MIN:\n            return False\n", ""),
    ("review: so many waiting still wait a week", REVIEW, "        if count >= 2 * REVIEW_SIZE:\n            return True\n", ""),
    ("review: the week is not counted from the last review", REVIEW, "return max(self.last_review, min(item.at for item in waiting)) if waiting else None", "return min(item.at for item in waiting) if waiting else None"),
    ("review: the week is counted from the newest note", REVIEW, "max(self.last_review, min(item.at for item in waiting))", "max(self.last_review, max(item.at for item in waiting))"),
    ("review: a day is a week", REVIEW, "return start is not None and now - start >= REVIEW_EVERY_DAYS * DAY", "return start is not None and now - start >= DAY"),
    ("review: the next review is not a week on", REVIEW, "return 0.0 if start is None else start + REVIEW_EVERY_DAYS * DAY", "return 0.0 if start is None else start"),
    # ---- the batch
    ("review: the surest are picked first", REVIEW, "for item in sorted(state.waiting(), key=lambda item: (item.score, item.at, item.id)):", "for item in sorted(state.waiting(), key=lambda item: (-item.score, item.at, item.id)):"),
    ("review: the newest first among equals", REVIEW, "return sorted(shown, key=lambda item: (item.score, item.at, item.id))", "return sorted(shown, key=lambda item: (item.score, -item.at, item.id))"),
    ("review: a batch has no size", REVIEW, "                if len(shown) >= size:\n                    break\n", ""),
    ("review: a batch shows the words as written", REVIEW, "                view = replace(item, title=entry.title, text=entry.text, tags=tuple(entry.tags), kind=entry.kind, scope=entry.scope, project=entry.project)\n", "                view = item\n"),
    ("review: a batch shows a forgotten note", REVIEW, "                if entry is None or not entry.active:\n                    if learner is not None:", "                if entry is None:\n                    if learner is not None:"),
    ("review: the scores are not made again before picking", REVIEW, "                for item in state.waiting():\n                    learned = learner.score(item)\n                    state.items[item.id] = replace(item, learned=learned, score=combined(item.llm, learned, ready))\n", "                pass\n"),
    ("review: the user's drop on the memory page is no lesson", REVIEW, "        return (_sample(item, entry), False) if by == \"user\" else None\n", "        return None\n"),
    ("review: an agent's forgetting is the user's lesson", REVIEW, "        by = \"user\" if entry.editor == \"user\" else \"auto\" if entry.editor == APP else \"agent\"\n", "        by = \"user\"\n"),
    # ---- the user's word
    ("review: a dropped note is not forgotten", REVIEW, "                    store.forget(note_id, by=by)\n", "                    pass\n"),
    ("review: a dropped note's words come again", REVIEW, "                    state.remember(item.digest, digest_of(entry.title, entry.text))\n                    lessons.append((_sample(item, entry), False))\n", "                    lessons.append((_sample(item, entry), False))\n"),
    ("review: the edits are not made", REVIEW, "                    alive[note_id] = _edit(store, alive[note_id], changes[note_id], state.items[note_id], by)\n", "                    pass\n"),
    ("review: the words as written are learned, not the kept ones", REVIEW, "                    lessons.append((_sample(item, alive[note_id]), True))\n", "                    lessons.append((_sample(item), True))\n"),
    ("review: a kept note is learned as dropped", REVIEW, "                    lessons.append((_sample(item, alive[note_id]), True))\n", "                    lessons.append((_sample(item, alive[note_id]), False))\n"),
    ("review: a note kept and dropped at once is decided", REVIEW, "                if note_id in torn or item is None or item.state != PENDING:", "                if item is None or item.state != PENDING:"),
    ("review: a decided note is decided again", REVIEW, "                if note_id in torn or item is None or item.state != PENDING:", "                if note_id in torn or item is None:"),
    ("review: an edit that changes nothing makes a revision", REVIEW, "    if not wanted:\n        return entry\n", ""),
    ("review: a global note cannot go to its project", REVIEW, "    move_to = (item.project or None) if wanted.get(\"scope\") == \"project\" else None\n", "    move_to = None\n"),
    ("review: the review is not noted", REVIEW, "            state.last_review, state.skipped = stamp, 0\n", "            state.skipped = 0\n"),
    ("review: the unsure count is not started again", REVIEW, "            state.last_review, state.skipped = stamp, 0\n", "            state.last_review = stamp\n"),
    ("review: nothing is learned", REVIEW, "                learner.learn_all(lessons, now=stamp)\n", "                pass\n"),
    ("review: no tidying after a review", REVIEW, "            tidied = self.tidy(store, learner, stamp)\n", "            tidied = {\"autoKept\": 0, \"autoDropped\": 0}\n"),
    ("review: the history is not written", REVIEW, "            state.history.append(\n", "            [].append(\n"),
    # ---- tidying
    ("review: a learner that knows little tidies", REVIEW, "        if not learner.ready:\n            return done\n", ""),
    ("review: tidying drops what it is not sure of", REVIEW, "                if learned < DROP_BELOW:\n                    try:", "                if learned < 0.5:\n                    try:"),
    ("review: tidying keeps what it is not sure of", REVIEW, "                elif learned >= KEEP_ABOVE:", "                elif learned >= 0.5:"),
    ("review: tidying does not forget what it drops", REVIEW, "                        store.forget(item.id, by=APP)\n", "                        pass\n"),
    ("review: tidying takes a note the user forgot", REVIEW, "                if entry is None or not entry.active:\n                    continue  # (the next batch", "                if entry is None:\n                    continue  # (the next batch"),
    ("review: tidied words are written again", REVIEW, "                    state.remember(item.digest, digest_of(entry.title, entry.text))\n                    done[\"autoDropped\"] += 1\n", "                    done[\"autoDropped\"] += 1\n"),
    # ---- the file
    ("review: each review has a lock of its own", REVIEW, "        self._lock = file_lock(self.path)\n", "        self._lock = __import__(\"threading\").RLock()\n"),
    ("review: decided notes stay for ever", REVIEW, "            state.items = {note_id: item for note_id, item in state.items.items() if item.state == PENDING or item.decided_at >= horizon}\n", "            state.items = dict(state.items)\n"),
    ("review: a waiting note leaves the file", REVIEW, "if item.state == PENDING or item.decided_at >= horizon}", "if item.decided_at >= horizon}"),
    ("review: a note waiting already is put up again", REVIEW, "            if entry.id in state.items:\n                return state.items[entry.id]\n", ""),
    ("review: a global note does not know its conversation's project", REVIEW, "entry.project or (project or \"\")", "entry.project"),
    ("review: the learner has no say", REVIEW, "    return 0.5 * llm + 0.5 * learned if ready else llm\n", "    return llm\n"),
    ("review: the learner has a say before it knows enough", REVIEW, "    return 0.5 * llm + 0.5 * learned if ready else llm\n", "    return 0.5 * llm + 0.5 * learned\n"),
    # ---- from the suggestions of before
    ("review: moved suggestions are taken for refused", REVIEW, "        proposals.discard(proposal.id)\n        moved += 1\n", "        proposals.dismiss(proposal.id)\n        moved += 1\n"),
    ("review: what the user refused before is forgotten", REVIEW, "    review.remember(proposals.dismissed())\n", ""),
    ("review: a full memory loses the suggestion", REVIEW, "            if rejected.code in (\"limit\", \"busy\"):\n                continue  # (it can move another time)\n", ""),
    ("review: a move that stopped half way is not finished", REVIEW, "            entry = found  # (written by a move that stopped half way)\n", "            proposals.discard(proposal.id)\n            continue\n"),
    ("review: a moved suggestion is sure", REVIEW, "score=combined(0.5, learned, ready), llm=0.5, learned=learned,", "score=0.9, llm=0.9, learned=learned,"),
    # ---- the curator writes
    ("curator: everything is written however unsure", CURATOR, "            if score < WRITE_AT:\n                unsure += 1\n                continue\n", ""),
    ("curator: the unsure are not counted", CURATOR, "        if unsure:\n            review.skip(unsure)\n", ""),
    ("curator: one half is sure enough", CURATOR, "WRITE_AT = 0.55\n", "WRITE_AT = 0.5\n"),
    ("curator: no allowance a day", CURATOR, "min(MAX_NOTES, AUTO_PER_DAY - written, MAX_PENDING - waiting)", "min(MAX_NOTES, MAX_PENDING - waiting)"),
    ("curator: no limit of the waiting notes", CURATOR, "min(MAX_NOTES, AUTO_PER_DAY - written, MAX_PENDING - waiting)", "min(MAX_NOTES, AUTO_PER_DAY - written)"),
    ("curator: asked though nothing could be written", CURATOR, "            if not self._room():\n                return []", "            if False:\n                return []"),
    ("curator: a failed look at the review is raised", CURATOR, "        except Exception:\n            log.warning(\"the memory curator could not read the review\", exc_info=True)\n            return []\n", "        except ZeroDivisionError:\n            return []\n"),
    ("curator: one answer goes past the day's allowance", CURATOR, "            if len(written) >= room:\n                break\n", ""),
    ("curator: dropped words are written again", CURATOR, "            if digest_of(note.title, note.text) in refused:\n                continue\n", ""),
    ("curator: refused suggestions are written", CURATOR, "        refused = review.digests() | set(self.proposals.dismissed())\n", "        refused = review.digests()\n"),
    ("curator: near-duplicates are written", CURATOR, "            if any(_same(gist, other) for other in known):\n                continue\n", ""),
    ("curator: near-duplicates of the memory's notes are written", CURATOR, "        known = [_gist(c.title, c.text) for c in self.store.candidates(exchange.project or None)]\n", "        known = []\n"),
    ("curator: near-duplicates of the review's notes are written", CURATOR, "        known += [_gist(item.title, item.text) for item in review.recent(now - RECENT_DAYS * DAY)]\n", ""),
    ("curator: what was just written is not compared", CURATOR, "            known.append(gist)\n", ""),
    ("curator: Step 1 and Step 2 are one thing", CURATOR, "    if a[1] and b[1] and a[1] != b[1]:\n        return False\n", ""),
    ("curator: a title without numbers is another thing", CURATOR, "    if a[1] and b[1] and a[1] != b[1]:", "    if a[1] != b[1]:"),
    ("curator: near means the same words", CURATOR, "NEAR_DUPLICATE = 0.7\n", "NEAR_DUPLICATE = 1.0\n"),
    ("curator: lately is for ever", CURATOR, "RECENT_DAYS = 14\n", "RECENT_DAYS = 10000\n"),
    ("curator: the model's word is not asked for", CURATOR, "            llm = confidence_of(note.raw.get(\"confidence\"))\n", "            llm = 0.9\n"),
    ("curator: the learner has no say", CURATOR, "            score = combined(llm, learned, ready)\n", "            score = llm\n"),
    ("curator: the note is the user's", CURATOR, "kind=note.kind, tags=note.tags, by=APP)", "kind=note.kind, tags=note.tags, by=\"user\")"),
    ("curator: a project note goes without its project", CURATOR, "            project = exchange.project if note.scope == \"project\" else \"\"\n", "            project = \"\"\n"),
    ("curator: the review does not know whose conversation it was", CURATOR, "                entry, engine=exchange.engine, conversation=exchange.conversation, row=exchange.row, why=note.why,\n", "                entry, engine=exchange.engine, conversation=\"\", row=\"\", why=\"\",\n"),
    ("curator: the review does not know the conversation's project", CURATOR, "project=project_path(exchange.project) if exchange.project else \"\",", "project=\"\","),
    ("curator: the prompt shows nothing the user kept", CURATOR, "            exchange, known, kept=self.learner.examples(True, EXAMPLES, project=exchange.project),\n", "            exchange, known, kept=(),\n"),
    ("curator: the prompt shows other projects' notes", CURATOR, "kept=self.learner.examples(True, EXAMPLES, project=exchange.project),", "kept=self.learner.examples(True, EXAMPLES),"),
    ("curator: the prompt shows nothing the user dropped", CURATOR, "            dropped=self.learner.examples(False, EXAMPLES, project=exchange.project), confidence=True,\n", "            dropped=(), confidence=True,\n"),
    ("curator: the model is not asked how sure it is", CURATOR, "project=exchange.project), confidence=True,", "project=exchange.project), confidence=False,"),
    ("curator: what the user kept is not shown", CURATOR, "        lines += [\"The user kept notes like:\", *map(_example_line, kept)]\n", "        pass\n"),
    ("curator: a long example is not cut", CURATOR, "    if len(text) > EXAMPLE_TEXT:\n        text = text[: EXAMPLE_TEXT - 1] + \"…\"\n", ""),
    ("curator: a percentage is not read", CURATOR, "number = float(text[:-1]) / 100 if text.endswith(\"%\") else float(text)", "number = float(text)"),
    ("curator: more than sure is believed", CURATOR, "    return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.5\n", "    return max(0.0, number) if math.isfinite(number) else 0.5\n"),
    ("curator: true is sure", CURATOR, "    if isinstance(value, bool):\n        return 0.5\n", ""),
    ("curator: a review without a learner has none", CURATOR, "        self.learner = learner if learner is not None or review is None else Learner(review.path.parent)\n", "        self.learner = learner\n"),
    # ---- the page's side
    ("bridge: the application's notes are news", BRIDGE, "        news = [c for c in changes if c.by != APP]\n", "        news = changes\n"),
    ("bridge: the application's changes are listed as the agents'", BRIDGE, "        agent_changes = [c for c in agent_changes if c.by != APP]\n", ""),
    ("bridge: the application is unknown", BRIDGE, "        if by == APP:\n            return {\"kind\": \"app\"}\n", ""),
    ("bridge: the badge counts a review that is not due", BRIDGE, "            state[\"pending\"] = status.pending if status.due else 0\n", "            state[\"pending\"] = status.pending\n"),
    ("bridge: review mode is ignored", BRIDGE, "        if self.review_mode:\n", "        if False:\n"),
    ("bridge: a note may be kept and dropped at once", BRIDGE, "        if set(keep) & set(drop):\n            raise MemoryRejected(\"bad_request\", \"A note cannot be kept and dropped at once.\")\n", ""),
    ("bridge: any field can be changed", BRIDGE, "            if key not in _EDITABLE:\n", "            if False:\n"),
    ("bridge: tags may be anything", BRIDGE, "            if key == \"tags\" and not (isinstance(field, list) and all(isinstance(tag, str) for tag in field)):\n", "            if False:\n"),
    ("bridge: a title may be a number", BRIDGE, "            if key != \"tags\" and not isinstance(field, str):\n", "            if False:\n"),
    ("bridge: ids may be anything in a list", BRIDGE, "    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):\n", "    if not isinstance(value, list):\n"),
    ("bridge: the edits may be anything", BRIDGE, "    if not isinstance(value, Mapping):\n        raise MemoryRejected(\"bad_request\", \"edits must map note ids to changes.\")\n", ""),
    ("bridge: the size of a batch is not checked", BRIDGE, "        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= REVIEW_SIZE:\n", "        if False:\n"),
    ("bridge: the score is not rounded", BRIDGE, "        score = round(item.score, 2)\n", "        score = item.score\n"),
    ("bridge: the suggestion goes the other way", BRIDGE, "            \"suggestKeep\": score >= 0.5,\n", "            \"suggestKeep\": score < 0.5,\n"),
    ("bridge: no reasons", BRIDGE, "            \"explain\": self.learner.explain(item),\n", "            \"explain\": [],\n"),
    ("bridge: the time of a note is in seconds", BRIDGE, "            \"at\": _ms(item.at),\n            \"suggestKeep\"", "            \"at\": int(item.at),\n            \"suggestKeep\""),
    ("bridge: the notes left are not told", BRIDGE, "            \"left\": done[\"pending\"],\n", ""),
    ("bridge: the next review is in seconds", BRIDGE, "            \"next\": _ms(done[\"next\"]) if done[\"next\"] else 0,\n", "            \"next\": done[\"next\"],\n"),
    ("bridge: the review state has no next", BRIDGE, "            \"next\": _ms(status.next) if status.next else 0,\n", "            \"next\": 0,\n"),
    ("bridge: an application's note is the user's in a search", BRIDGE, "else APP if e.source == APP else \"user\") == engine]", "else \"user\") == engine]"),
    ("store: the application's notes are indexed as the user's", STORE, "else APP if entry.source == APP else \"user\"\n", "else \"user\"\n"),
    # ---- what the user asks for in so many words
    ("launch: a note the user asked for teaches nothing", LAUNCH, "                remembered(self.learner, entry, event.engine)\n", "                pass\n"),
    ("launch: the learning switch is not looked at", LAUNCH, "        if self.learn():\n", "        if True:\n"),
    ("launch: a failing learner stops the note", LAUNCH, "            except Exception:\n                log.warning(\"the memory could not learn from a note the user asked for\", exc_info=True)\n", "            except ZeroDivisionError:\n                pass\n"),
    ("launch: the learner is not the memory folder's", LAUNCH, "        self.learner = Learner(self.root)\n", "        self.learner = Learner(self.root / \"elsewhere\")\n"),
    ("launch: the review is not the memory folder's", LAUNCH, "        self.review = ReviewStore(self.root)\n", "        self.review = ReviewStore(self.root / \"elsewhere\")\n"),
    # ---- the suggestions of before
    ("proposals: taking off remembers the words", PROPOSALS, "            self._write([p for p in pending if p.id != proposal_id], dismissed)\n            return True\n", "            self._write([p for p in pending if p.id != proposal_id], [*dismissed, *(p.digest for p in pending if p.id == proposal_id)])\n            return True\n"),
    ("proposals: taking off leaves the suggestion", PROPOSALS, "            self._write([p for p in pending if p.id != proposal_id], dismissed)\n            return True\n", "            return True\n"),
    ("proposals: the refused words are not told", PROPOSALS, "            return self._read()[1]\n", "            return []\n"),
]


def load(path: Path) -> tuple[bytes, str, bool]:
    """The file's bytes, its text with plain newlines, and whether it had Windows ones (written back the same way)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return raw, text.replace("\r\n", "\n"), "\r\n" in text


def copy_tree() -> Path:
    work = Path(tempfile.mkdtemp(prefix="wittycode-mutate-review-"))
    shutil.copytree(ROOT / "desktop", work / "desktop", ignore=shutil.ignore_patterns("__pycache__", "ui_dist"))
    shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
    return work


def pytest(work: Path, files: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *files], cwd=work, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args
    only = {int(a) for a in args if a.isdigit()}
    missing = []
    for number, (name, path, old, _new) in enumerate(M, 1):
        count = load(ROOT / path)[1].count(old)
        if count != 1:
            print(f"[{number}] {name}: PATTERN FOUND {count} TIMES", flush=True)
            missing.append(name)
    if check_only or missing:
        print(f"{len(M) - len(missing)} of {len(M)} patterns found once")
        return 1 if missing else 0
    work = copy_tree()
    try:
        probe = subprocess.run([PY, "-c", "import desktop; print(desktop.__file__)"], cwd=work, env={**os.environ, "PYTHONPATH": str(work)}, capture_output=True, text=True)
        if not Path(probe.stdout.strip()).is_relative_to(work):
            print("the copy is not the package that is imported:", probe.stdout.strip(), probe.stderr.strip())
            return 2
        everything = sorted({test for files in TESTS.values() for test in files})
        clean = pytest(work, everything)
        if clean.returncode != 0:
            print("the tests fail without a mutant:\n" + clean.stdout[-3000:])
            return 2
        survivors = []
        ran = 0
        for number, (name, path, old, new) in enumerate(M, 1):
            if only and number not in only:
                continue
            target = work / path
            raw, text, crlf = load(target)
            mutated = text.replace(old, new)
            target.write_bytes((mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))
            started = time.time()
            try:
                done = pytest(work, TESTS[path])
                caught = done.returncode != 0
                tail = (done.stdout.strip().splitlines() or ["?"])[-1]
            except subprocess.TimeoutExpired:
                caught, tail = True, "timeout (hangs: caught)"
            finally:
                target.write_bytes(raw)
            ran += 1
            print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
            if not caught:
                survivors.append(name)
        print(f"\n{ran - len(survivors)} of {ran} caught")
        for name in survivors:
            print("  survivor:", name)
        return 1 if survivors else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
