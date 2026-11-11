"""Mutation checks for the importers (core/importers) and the origin of a saved harness conversation.

    python sandbox/mutate_importers.py            run every mutant
    python sandbox/mutate_importers.py --check    only check that every pattern is found exactly once
    python sandbox/mutate_importers.py 12 30      run only these mutants (by number)

Each mutant replaces one fragment of a file, runs the importer tests, and puts the file back byte for byte.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
PKG = ROOT / "desktop" / "core" / "importers"
ZSTD, SPLIT, BASE = PKG / "zstd.py", PKG / "split.py", PKG / "base.py"
CLAUDE, CODEX, DEVIN, FACTORY, CONVERT = PKG / "claude.py", PKG / "codex.py", PKG / "devin.py", PKG / "factory.py", PKG / "convert.py"
STORE = ROOT / "desktop" / "core" / "harness" / "store.py"
TESTS = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_importers_*.py")) + ["tests/test_harness_origin.py"]

M = [
    ("zstd: the end mark bit is read as data", ZSTD, "self.left = len(data) * 8 - (9 - data[-1].bit_length())", "self.left = len(data) * 8 - (8 - data[-1].bit_length())"),
    ("zstd: a literal-less sequence does not shift the repeated offsets", ZSTD, "which = offset - 1 if length else offset", "which = offset - 1"),
    ("zstd: the third repeated offset is not rotated", ZSTD, "if which > 1:\n                        offsets[2] = offsets[1]", "if which > 2:\n                        offsets[2] = offsets[1]"),
    ("zstd: the FSE spread step is wrong", ZSTD, "step = (size >> 1) + (size >> 3) + 3", "step = (size >> 1) + (size >> 3) + 5"),
    ("zstd: direct Huffman weights low nibble first", ZSTD, "weights.append(byte >> 4)\n            weights.append(byte & 15)", "weights.append(byte & 15)\n            weights.append(byte >> 4)"),
    ("zstd: the implied last Huffman weight is off by one", ZSTD, "weights = weights + [left.bit_length()]", "weights = weights + [left.bit_length() + 1]"),
    ("zstd: four literal streams of equal size", ZSTD, "sizes = [part, part, part, size - 3 * part]", "sizes = [part, part, part, part]"),
    ("zstd: only one skippable magic number", ZSTD, "if magic & 0xFFFFFFF0 == SKIPPABLE:", "if magic == SKIPPABLE:"),
    ("zstd: the checksum is read as the next frame", ZSTD, "    if checksum:\n        _read(stream, 4)", "    if False:\n        _read(stream, 4)"),
    ("zstd: an RLE block one byte too long", ZSTD, "data = _read(stream, 1) * size", "data = _read(stream, 1) * (size + 1)"),
    ("zstd: an overlapping match is copied once", ZSTD, "history += (pattern * (match // offset + 1))[:match]", "history += pattern[:match]"),
    ("zstd: the two-byte literal size is misread", ZSTD, "size, used = (head >> 4) | (block[1] << 4), 2", "size, used = (head >> 4) | (block[1] << 3), 2"),
    ("zstd: a damaged Huffman stream passes", ZSTD, "    if (position - 1) * 8 - have != (len(data) - 1) * 8:\n        raise ZstdError", "    if False:\n        raise ZstdError"),
    ("zstd: the window keeps too little", ZSTD, "excess = len(self.history) - self.window", "excess = len(self.history) - self.window // 64"),
    ("split: nothing is merged", SPLIT, "MERGE_LIMIT = 1200", "MERGE_LIMIT = 0"),
    ("split: a heading in a code block is a heading", SPLIT, "found = None if inside else _HEADING.match(line)", "found = _HEADING.match(line)"),
    ("split: comments in code blocks are stripped", SPLIT, "return \"\\n\".join(run if inside else _COMMENT.sub(\"\", run) for inside, run in _outside_fences(text))", "return _COMMENT.sub(\"\", text)"),
    ("split: import lines stay", SPLIT, "if not _IMPORT_ONLY.match(line)", "if True"),
    ("split: a table of contents is imported", SPLIT, "if not body.strip() or _only_contents(body):", "if not body.strip():"),
    ("split: secrets are not scrubbed", SPLIT, "body = scrub(drop_imports(strip_comments(body)), report)", "body = drop_imports(strip_comments(body))"),
    ("split: parts are not numbered", SPLIT, "name = _fit_title(heading, f\" ({number})\" if number > 1 else \"\")", "name = _fit_title(heading)"),
    ("split: a long code block is cut open", SPLIT, "        if current:\n            pieces.append(\"\\n\".join([opener, *current, closer]))", "        if current:\n            pieces.append(\"\\n\".join([opener, *current]))"),
    ("split: the heading says nothing about the kind", SPLIT, "        if pattern.search(heading):", "        if False:"),
    ("split: tags lose the app", SPLIT, "for tag in [app, *base,", "for tag in [*base,"),
    ("split: the same titles are not told apart", SPLIT, "        if _clashes(title, names):", "        if False:"),
    ("split: repeated texts are kept", SPLIT, "        if candidate.digest in seen:\n            continue", "        if False:\n            continue"),
    ("split: keys ignore the file", SPLIT, "raw = \"\\n\".join([app, norm_key(origin_file), \" › \".join(path), str(part), str(occurrence)])", "raw = \"\\n\".join([app, \" › \".join(path), str(part), str(occurrence)])"),
    ("split: keys ignore a repeated heading", SPLIT, "raw = \"\\n\".join([app, norm_key(origin_file), \" › \".join(path), str(part), str(occurrence)])", "raw = \"\\n\".join([app, norm_key(origin_file), \" › \".join(path), str(part)])"),
    ("split: front matter stays", SPLIT, "    _, body = front_matter(text)\n    body = scrub(", "    body = text\n    body = scrub("),
    ("split: a tight list gets blank lines", SPLIT, "            elif _LIST_ITEM.match(line) and current:\n                end(\"\\n\")", "            elif _LIST_ITEM.match(line) and current:\n                end(\"\\n\\n\")"),
    ("split: a lone top heading is not the title", SPLIT, "    while not title.strip() and not section.own()", "    while False and not section.own()"),
    ("base: a tool call is a message of its own", BASE, "        if last is not None and last.role == \"assistant\":\n            self.messages[-1] = Message(\"assistant\", f\"{last.text}\\n{line}\", last.at)", "        if False:\n            self.messages[-1] = Message(\"assistant\", f\"{last.text}\\n{line}\", last.at)"),
    ("base: streamed parts of a message are messages", BASE, "if key is not None and key == self._key and last is not None", "if False and key == self._key and last is not None"),
    ("base: secrets in the user's words are kept", BASE, "    def user(self, text: str, at: float | None) -> None:\n        text = scrub(text, self.report).strip()", "    def user(self, text: str, at: float | None) -> None:\n        text = text.strip()"),
    ("base: a tool subject with a secret is kept", BASE, "        line = tool_line(name, subject)\n        if find_secret(line):", "        line = tool_line(name, subject)\n        if False:"),
    ("base: the body of a private key is kept", BASE, "            if \"PRIVATE KEY-----\" in line and not _PRIVATE_KEY_END.search(line):\n                in_key = True", "            if \"PRIVATE KEY-----\" in line and not _PRIVATE_KEY_END.search(line):\n                in_key = False"),
    ("base: milliseconds are seconds", BASE, "while number > 1e11:", "while number > 1e15:"),
    ("base: time zones are ignored", BASE, "return (moment.replace(tzinfo=_dt.timezone.utc) - offset).timestamp()", "return moment.replace(tzinfo=_dt.timezone.utc).timestamp()"),
    ("base: the head limit is ignored", BASE, "            if limit is not None and seen >= limit:\n                return\n            seen += len(line)\n", "            if False:\n                return\n            seen += len(line)\n"),
    ("base: the cut first line of a tail is kept", BASE, "    if start > 0:\n        parts = parts[1:]", "    if False:\n        parts = parts[1:]"),
    ("base: a database without a log is opened so that files appear", BASE, "return uri + (\"?mode=ro\" if Path(f\"{path}-wal\").exists() else \"?mode=ro&immutable=1\")", "return uri + \"?mode=ro\""),
    ("base: imported ids differ every time", BASE, "hashlib.sha1(key.encode('utf-8')).hexdigest()[:28]", "hashlib.sha1(repr(object()).encode('utf-8')).hexdigest()[:28]"),
    ("claude: meta lines are the user's words", CLAUDE, "if record.get(\"isSidechain\") or record.get(\"isMeta\") or", "if record.get(\"isSidechain\") or"),
    ("claude: the generated title beats the rename", CLAUDE, "return first_words(self.custom or self.ai or self.summary)", "return first_words(self.ai or self.custom or self.summary)"),
    ("claude: synthetic API errors are answers", CLAUDE, "if record.get(\"isApiErrorMessage\") or message.get(\"model\") == \"<synthetic>\":", "if False:"),
    ("claude: a slash command is imported as its wrapper", CLAUDE, "        if command:\n", "        if False:\n"),
    ("claude: projects come from the folder names", CLAUDE, "            cwd = self._cwd(path)\n            if cwd:\n                seen", "            cwd = path.parent.name\n            if cwd:\n                seen"),
    ("claude: auto memory of an unknown folder is imported", CLAUDE, "            if not folder:\n", "            if False:\n"),
    ("claude: topic types give no kind", CLAUDE, "kind = MEMORY_TYPES.get(meta.get(\"type\", \"\").strip().lower())", "kind = None"),
    ("claude: index lines are imported", CLAUDE, "text = _without_index_lines(text, siblings)", "text = text"),
    ("claude: set-aside copies are listed", CLAUDE, "\".orphaned-\" not in name and ", ""),
    ("claude: the state file's projects are not read", CLAUDE, "        for folder in self._listed_projects():", "        for folder in []:"),
    ("codex: AGENTS.md context is the user's words", CODEX, "    if stripped.startswith(\"# AGENTS.md instructions for \"):\n        return None", "    if False:\n        return None"),
    ("codex: only listed tags are added context", CODEX, "if tag.lower() in _ADDED_TAGS or \"_\" in tag or \"-\" in tag:", "if tag.lower() in _ADDED_TAGS:"),
    ("codex: an answer to a question stays wrapped", CODEX, "        if tag.lower() in _UNWRAP:\n", "        if False:\n"),
    ("codex: waiting calls get lines", CODEX, "if name not in QUIET_TOOLS:", "if True:"),
    ("codex: sub-agent threads are listed", CODEX, "if reading.subagent or (exact and not reading.builder.messages):", "if exact and not reading.builder.messages:"),
    ("codex: the compressed copy is preferred", CODEX, "rank = rank_folder * 2 + (1 if is_compressed(path) else 0)", "rank = rank_folder * 2 + (0 if is_compressed(path) else 1)"),
    ("codex: the thread list's title beats the thread name", CODEX, "title = name or as_text(row.get(\"name\")).strip() or as_text(row.get(\"title\")).strip()", "title = as_text(row.get(\"title\")).strip() or name or as_text(row.get(\"name\")).strip()"),
    ("codex: a patch is its first line", CODEX, "    if isinstance(arguments, str) and \"*** Begin Patch\" in arguments:\n        return \", \".join(_PATCH_FILE.findall(arguments))", "    if isinstance(arguments, str) and \"*** Begin Patch\" in arguments:\n        return first_line(arguments)"),
    ("codex: compressed files are opened for the list", CODEX, "            elif not (title and cwd and row):", "            else:"),
    ("codex: developer messages are the user's", CODEX, "            if role == \"user\":\n                self.builder.user(_user_text(", "            if role in (\"user\", \"developer\"):\n                self.builder.user(_user_text("),
    ("codex: the oldest format is not read", CODEX, "        if kind in _RESPONSE_TYPES and payload is None:", "        if False:"),
    ("devin: compactions are not followed", DEVIN, "if depth < 32 and isinstance(source, int) and source in nodes and source not in seen:", "if False:"),
    ("devin: a summarizer's request is the user's", DEVIN, "            if marked and meta.get(\"is_user_input\") is not True:", "            if False:"),
    ("devin: hidden sessions are listed", DEVIN, "                if hidden:\n", "                if False:\n"),
    ("devin: a lock is called unreadable", DEVIN, "return LOCKED if \"locked\" in text.lower() or \"busy\" in text.lower() else UNREADABLE.format(error=text)", "return UNREADABLE.format(error=text)"),
    ("devin: conversation dumps are notes", DEVIN, "            if _DUMP.match(text.lstrip()):", "            if False:"),
    ("devin: waiting calls get lines", DEVIN, "if isinstance(call, dict) and as_text(call.get(\"name\")) not in QUIET_TOOLS:", "if isinstance(call, dict):"),
    ("factory: hidden messages are imported", FACTORY, "        if is_hidden_message(message_id or None, as_text(record.get(\"visibility\")) or None):", "        if False:"),
    ("factory: system reminders are the user's words", FACTORY, "if not is_system_reminder(text)", "if True"),
    ("factory: sub-agent sessions are listed", FACTORY, "if reading.child or not reading.builder.messages:", "if not reading.builder.messages:"),
    ("factory: the folders Droid also reads are not", FACTORY, "for folder in (self.root, self.home / \".agents\", self.home / \".agent\"):", "for folder in (self.root,):"),
    ("convert: every message is a turn", CONVERT, "if message.role == \"user\" or not turns:", "if True:"),
    ("convert: a turn ends where it starts", CONVERT, "turns[-1].finished = max(turns[-1].finished or at, at)", "turns[-1].finished = turns[-1].finished"),
    ("convert: no origin", CONVERT, "origin={\"app\": info.app, \"native\": info.native_id, \"at\": imported},", "origin={},"),
    ("convert: answers are user messages", CONVERT, "items.append(user_message(text) if message.role == \"user\" else assistant_item(text))", "items.append(user_message(text))"),
    ("store: the origin is not written", STORE, "    if session.origin:\n        data[\"origin\"] = session.origin", "    if False:\n        data[\"origin\"] = session.origin"),
    ("store: an empty origin is written", STORE, "    if session.origin:\n        data[\"origin\"]", "    if True:\n        data[\"origin\"]"),
    ("store: the origin is not read", STORE, "origin=dict(raw[\"origin\"]) if isinstance(raw.get(\"origin\"), Mapping) else {},", "origin={},"),
]


def patterns_for(raw: bytes, old: str, new: str) -> tuple[bytes, bytes]:
    """The fragments as bytes in the file's own line ends."""
    if b"\r\n" in raw:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    return old.encode("utf-8"), new.encode("utf-8")


args = sys.argv[1:]
check_only = "--check" in args
only = {int(a) for a in args if a.isdigit()}
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    if only and number not in only:
        continue
    raw = path.read_bytes()
    old_bytes, new_bytes = patterns_for(raw, old, new)
    if raw.count(old_bytes) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {raw.count(old_bytes)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    started = time.time()
    try:
        path.write_bytes(raw.replace(old_bytes, new_bytes))
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_bytes(raw)
    assert path.read_bytes() == raw
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
    if not caught:
        survivors.append(name)

total = len([1 for n in range(1, len(M) + 1) if not only or n in only])
print(f"\n{total - len(survivors)} of {total} caught" if not check_only else f"{total - len(survivors)} of {total} patterns found once")
for name in survivors:
    print("  survivor:", name)
sys.exit(1 if survivors else 0)
