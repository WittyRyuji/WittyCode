"""Mutation checks for the harness re-review fixes: a wildcard that picks a link at any depth, and links ripgrep follows (F1); a stop
racing the final commit of a write (F2); the programs a repository makes Git run (F4); network and device paths (F5);
a glob that backtracks and braces that explode (F6).

    python sandbox/mutate_harness_rereview.py            run every mutant
    python sandbox/mutate_harness_rereview.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
H = ROOT / "desktop" / "core" / "harness"
SHELL, PERM, TOOLS, FS = H / "shell.py", H / "permissions.py", H / "tools.py", H / "fs_tools.py"
LINKS = ["tests/test_harness_rereview_links.py"]
CANCEL = ["tests/test_harness_rereview_cancel.py"]
GIT = ["tests/test_harness_rereview_git.py"]
PATHS = ["tests/test_harness_rereview_paths.py"]
GLOB = ["tests/test_harness_rereview_glob.py"]
R2_CANCEL = ["tests/test_harness_security_cancel.py"]
NESTED = ["tests/test_harness_nested_links.py"]

M = [
    # F1
    ("F1 a wildcard in the last segment is not expanded", SHELL, "    if index is None:\n        return []\n    folder = resolved_argument(", "    if index is None or index == len(segments) - 1:\n        return []\n    folder = resolved_argument(", LINKS),
    ("F1 a wildcard is expanded only when the argument itself is unknown", SHELL, "    return [resolved_argument(value, cwd), *_wildcard_targets(value, cwd)]", "    direct = resolved_argument(value, cwd)\n    return [direct] if direct is not None else [None, *_wildcard_targets(value, cwd)]", LINKS),
    ("F1 a match stands for the folder it was found in", SHELL, "            target = _resolved(folder.joinpath(name, *fixed))", "            target = _resolved(folder.joinpath(*fixed))", LINKS),
    ("F1 only the first wildcard segment is expanded", SHELL, "            if target is not None and following is not None:\n                pending.append((target, following))", "            if False:\n                pending.append((target, following))", LINKS),
    ("F1 a folder a wildcard passes through does not count", SHELL, "            targets.append(target)\n            if target is not None and following is not None:", "            if following is None:\n                targets.append(target)\n            if target is not None and following is not None:", LINKS),
    ("F1 the fixed segments after a wildcard are dropped", SHELL, "        fixed = segments[index + 1 : following]", "        fixed = []", LINKS),
    ("F1 the listing budget is not kept", SHELL, "        if listings > _EXPANSION_LISTINGS:\n            return [*targets, None]", "        if False:\n            return [*targets, None]", LINKS),
    ("F1 a spent listing budget counts as known", SHELL, "        if listings > _EXPANSION_LISTINGS:\n            return [*targets, None]", "        if listings > _EXPANSION_LISTINGS:\n            return targets", LINKS),
    ("F1 the target budget is not kept", SHELL, "            if len(targets) >= _EXPANSION_TARGETS:\n                return [*targets, None]", "            if False:\n                return [*targets, None]", LINKS),
    ("F1 a cut-off listing counts as complete", SHELL, "            targets.append(None)\n            names = names[:_LISTING_LIMIT]", "            names = names[:_LISTING_LIMIT]", LINKS),
    ("F1 read-only check ignores what a wildcard picks", PERM, "        for target in argument_targets(value, cwd):\n            if target is None or not fs_tools.is_within(target, project):", "        for target in argument_targets(value, cwd)[:1]:\n            if target is None or not fs_tools.is_within(target, project):", LINKS),
    ("F1 protected check ignores what a wildcard picks", SHELL, "            for target in argument_targets(piece, cwd):\n                yield piece, target", "            for target in argument_targets(piece, cwd)[:1]:\n                yield piece, target", LINKS),
    ("F1 rg may follow links", PERM, " or _follows_links(item) for item in rest):", " for item in rest):", LINKS),
    ("F1 rg --follow is missed", PERM, '    return item == "--follow" or (item.startswith("-")', '    return item == "--follow-links" or (item.startswith("-")', LINKS),
    ("F1 rg -L is missed", PERM, ' and not item.startswith("--") and "L" in item)', ' and not item.startswith("--") and False)', LINKS),
    ("F1 rg -l is taken for -L", PERM, ' and not item.startswith("--") and "L" in item)', ' and not item.startswith("--") and "l" in item.lower())', LINKS),
    ("F1 where /r is allowed", PERM, '        if name == "where" and any(item.lower() in ("/r", "-r") for item in rest):', "        if False:", LINKS),
    # F2
    ("F2 the replace attempts are not guarded", FS, "        with ctx.writes.commit(ctx.abort) as written:\n            try:\n                os.replace(temporary, path)", "        with contextlib.nullcontext(lambda: None) as written:\n            try:\n                os.replace(temporary, path)", CANCEL),
    ("F2 a stopped call still writes a temporary file", FS, "    _refuse_if_stopped(ctx)\n    try:\n        mode: int | None", "    try:\n        mode: int | None", CANCEL),
    ("F2 the in-place fallback is not guarded", FS, "    with ctx.writes.commit(ctx.abort) as written:\n        path.write_bytes(data)\n        written()", "    path.write_bytes(data)", CANCEL),
    ("F2 a replace is not recorded as written", FS, "                written()\n                return True", "                return True", CANCEL),
    ("F2 the commit ignores abort", TOOLS, "            if self.stopped or abort.is_set():", "            if self.stopped:", CANCEL),
    ("F2 the commit ignores a stop", TOOLS, "            if self.stopped or abort.is_set():", "            if abort.is_set():", CANCEL),
    ("F2 a stop does not mark the call stopped", TOOLS, "            self.stopped = True\n            return not (self.writing or self.written)", "            return not (self.writing or self.written)", CANCEL),
    ("F2 a stop always claims nothing was written", TOOLS, "            return not (self.writing or self.written)", "            return True", CANCEL),
    ("F2 a stop overlooks a write under way", TOOLS, "            return not (self.writing or self.written)", "            return not self.written", CANCEL),
    ("F2 the commit window is not marked", TOOLS, "            self.writing = True\n        try:", "        try:", CANCEL),
    ("F2 the commit window stays marked", TOOLS, "            with self._lock:\n                self.writing = False", "            with self._lock:\n                pass", CANCEL),
    ("F2 a write is not recorded", TOOLS, "        with self._lock:\n            self.written = True", "        with self._lock:\n            self.written = False", CANCEL),
    ("F2 a cancel does not stop the guard", TOOLS, "        if not call.writes.stop():\n            log.warning(", "        if False:\n            log.warning(", CANCEL),
    ("F2 a stop after the write began is not logged", TOOLS, '            log.warning("a call was stopped after its write had begun', '            log.debug("a call was stopped after its write had begun', CANCEL),
    ("F2 calls share one guard", TOOLS, "    call = replace(ctx, writes=CommitGuard())", "    call = replace(ctx)", CANCEL),
    # The previous round's mutants of the code F2 replaced (mutate_harness_security.py R2 "the handler shares the turn's
    # context" and "no in-place fallback"). The guard now refuses what the copy of the context used to, so sharing the
    # context shows only as a cancel that stops every later write: that round's tests cannot see it, this round's can.
    ("F2 the handler shares the turn's context", TOOLS, "    call = replace(ctx, writes=CommitGuard())", "    call = ctx", [*CANCEL, *R2_CANCEL]),
    ("F2 no in-place fallback", FS, "    with ctx.writes.commit(ctx.abort) as written:\n        path.write_bytes(data)\n        written()", '    raise PermissionError(13, "locked")', R2_CANCEL),
    # F4
    ("F4 Git starts without the neutral settings", SHELL, "    _git_env(env)\n    if IS_WINDOWS:", "    if IS_WINDOWS:", GIT),
    ("F4 fsmonitor is not neutralised", SHELL, '    ("core.fsmonitor", "false"),\n', "", GIT),
    ("F4 settings the environment carries are overwritten", SHELL, '        start = int(env.get("GIT_CONFIG_COUNT", "0"))', "        start = 0", GIT),
    ("F4 the count covers only the neutral settings", SHELL, '    env["GIT_CONFIG_COUNT"] = str(start + len(_GIT_NEUTRAL))', '    env["GIT_CONFIG_COUNT"] = str(len(_GIT_NEUTRAL))', GIT),
    ("F4 a broken count breaks every command", SHELL, "    except ValueError:\n        start = 0\n    for offset", "    except ZeroDivisionError:\n        start = 0\n    for offset", GIT),
    ("F4 a read-only Git command is not checked", PERM, "                setting = git_runs_own_programs(cwd) if _runs_git(command) else None", "                setting = None", GIT),
    ("F4 only a command that starts with Git is checked", PERM, '    return any(_program(token) == "git" for token in command_tokens(command))', '    return any(_program(token) == "git" for token in command_tokens(command)[:1])', GIT),
    ("F4 every read-only command is checked", PERM, '    return any(_program(token) == "git" for token in command_tokens(command))', "    return True", GIT),
    ("F4 a boolean counts as a program", PERM, "    if value.lower() in _GIT_NO_PROGRAM:\n        return False", "    if False:\n        return False", GIT),
    ("F4 an unknown section counts", PERM, "    if section not in _GIT_RUNS_PROGRAM:\n        return False\n    names = _GIT_RUNS_PROGRAM[section]", "    names = _GIT_RUNS_PROGRAM.get(section)", GIT),
    ("F4 any key of a known section counts", PERM, "    if names is not None and last not in names:\n        return False", "    if False:\n        return False", GIT),
    ("F4 keys keep their case", PERM, "        key = key.strip().lower()", "        key = key.strip()", GIT),
    ("F4 sections keep their case", PERM, "(name.lower(), sub.strip().strip('\"').lower())", "(name, sub.strip().strip('\"').lower())", GIT),
    ("F4 a key on the header's line is missed", PERM, "            line = line[end + 1 :].strip()", '            line = ""', GIT),
    ("F4 a key without a value is a program", PERM, '_git_value(value) if sep else "true"', "_git_value(value) if sep else key", GIT),
    ("F4 a comment stays in the value", PERM, '        elif char in "#;" and not quoted:\n            break', "        elif False:\n            break", GIT),
    ("F4 pager.<command> is missed", PERM, '    "pager": None,  # pager.<command> is itself a command, whatever it is called\n', "", GIT),
    ("F4 include.path is missed", PERM, '    "include": frozenset({"path"}),  # another file, which may hold any of the above\n', "", GIT),
    # Seven mutants of this round tested how a repository's Git metadata was read (its hooks folder, a linked tree's
    # gitdir, the shared configuration, an unreadable file); the G1/G2 round rewrote that reading, and its script
    # mutate_harness_g12.py mutates the new code for the same behaviours: "F4 a hooks folder always asks", "G1 a
    # relative core.hooksPath is read against the process folder", "G1 the .git file of a linked tree is not
    # followed", "G1 a relative gitdir is read against the process folder", "G1 the shared configuration of a linked
    # tree is not read", "F4 quotes stay in the value", and, because the configuration is now read through
    # _git_bytes, "G2 a file that cannot be read counts as absent". They are not repeated here.
    ("F4 config.worktree is not read", PERM, '    return [folder / name for folder in folders for name in ("config", "config.worktree")]', '    return [folder / "config" for folder in folders]', GIT),
    ("F4 a write into .git is allowed", PERM, '            if _git_internals(spec, args, ctx) is not None:\n                return Decision("ask"', '            if False:\n                return Decision("ask"', GIT),
    ("F4 approving edits approves .git", PERM, '            return "edit:" + os.path.normcase(str(internals.parent)) if internals is not None else "edit"', '            return "edit"', GIT),
    ("F4 one approval covers every Git folder", PERM, '            return "edit:" + os.path.normcase(str(internals.parent)) if internals is not None else "edit"', '            return "edit:.git" if internals is not None else "edit"', GIT),
    ("F4 .git is matched with its case", PERM, 'any(os.path.normcase(part) == ".git" for part in path.parts)', 'any(part == ".git" for part in path.parts)', GIT),
    # F5
    ("F5 a network path is resolved", FS, "    if is_remote_path(text):\n        raise ToolError(REMOTE_REFUSED.format(path=text))", "    if False:\n        raise ToolError(REMOTE_REFUSED.format(path=text))", PATHS),
    ("F5 only backslashes count as separators", FS, r'_REMOTE_PATH = re.compile(r"[\\/]{2}")', r'_REMOTE_PATH = re.compile(r"\\\\")', PATHS),
    ("F5 two separators anywhere count", FS, "    return _REMOTE_PATH.match(_unquoted(raw)) is not None", "    return _REMOTE_PATH.search(_unquoted(raw)) is not None", PATHS),
    ("F5 quotes hide a network path", FS, "    return _REMOTE_PATH.match(_unquoted(raw)) is not None", "    return _REMOTE_PATH.match(raw) is not None", PATHS),
    ("F5 decide leaves a network path to the level", PERM, "    remote = _remote_path(spec, args)\n    if remote is not None:", "    remote = None\n    if remote is not None:", PATHS),
    ("F5 an unknown tool's path is not checked", PERM, "    key = _PATH_ARGS.get(spec.name) or next((name for name in _FALLBACK_ARGS if _text(args, name)), None)\n    raw = _text(args, key) if key else None\n    return raw", "    key = _PATH_ARGS.get(spec.name)\n    raw = _text(args, key) if key else None\n    return raw", PATHS),
    # F6
    ("F6 brace expansion has no budget", FS, "    budget[0] -= 1\n    if budget[0] < 0:\n        return None", "    if False:\n        return None", GLOB),
    ("F6 every group gets a budget of its own", FS, "                    more = _expand_braces(head + part + tail, budget)", "                    more = _expand_braces(head + part + tail, [MAX_BRACE_STEPS])", GLOB),
    ("F6 a pattern of any length is expanded", FS, "    if len(pattern) > MAX_PATTERN_CHARS:\n        return [pattern]\n    budget", "    if False:\n        return [pattern]\n    budget", GLOB),
    ("F6 the filter matches with a backtracking regex", FS, '        rules.append((_glob_matcher(item), "/" in item))', r'        rules.append((re.compile("".join("[^/]*" if char == "*" else re.escape(char) for char in item) + r"\Z", re.IGNORECASE).match, "/" in item))', GLOB),
    ("F6 a star crosses folders", FS, ' and text.endswith(tail) and "/" not in text', " and text.endswith(tail)", GLOB),
    ("F6 the head and the tail may overlap", FS, "    return lambda text: len(text) >= least and ", "    return lambda text: ", GLOB),
    ("F6 a run cannot stand for nothing", FS, "        if index < len(steps) and steps[index][0] == _RUN:\n            stack.append(index + 1)", "        if False:\n            stack.append(index + 1)", GLOB),
    ("F6 a run stands for exactly one item", FS, "            if step[0] == _RUN:\n                moved.add(index)", "            if step[0] == _RUN:\n                moved.add(index + 1)", GLOB),
    ("F6 a negated class is not negated", FS, "    return any(low <= char <= high for low, high in ranges) != negated", "    return any(low <= char <= high for low, high in ranges)", GLOB),
    ("F6 a range is read as three characters", FS, '        if index + 2 < len(body) and body[index + 1] == "-":', "        if False:", GLOB),
    ("F6 the pattern keeps its case on Windows", FS, "    folded = pattern.lower() if IS_WINDOWS else pattern", "    folded = pattern", GLOB),
    ("F6 the name keeps its case on Windows", FS, "        if IS_WINDOWS:\n            text = text.lower()\n        if last_segment:", "        if last_segment:", GLOB),
    ("F6 **/x is matched against the whole path", FS, '            text = text.rsplit("/", 1)[-1]', "            pass", GLOB),
    ("F6 src/** matches src itself", FS, '    if len(parts) > 1 and parts[-1] == "**":\n        steps.insert(-1, ("segment", [(_RUN, None)]))', '    if False:\n        steps.insert(-1, ("segment", [(_RUN, None)]))', GLOB),
    ("F6 a bare name is matched against the whole path", FS, "        return any(matches(rel if anchored else base) for matches, anchored in rules)", "        return any(matches(rel) for matches, anchored in rules)", GLOB),
    # F1, the link nobody names (a recursive walk into it)
    ("F1b a walk that reads what it finds is read-only", PERM, "    if len(parts) == 2 and _reads_walked_files(parts[0], parts[1]):\n        return False\n", "", NESTED),
    ("F1b findstr /s is read-only", PERM, '        if name == "findstr" and any(item.lower().startswith("/s") for item in rest):\n            return False', "        if False:\n            return False", NESTED),
    ("F1b only -Recurse spelled out in full counts", PERM, 'r"-r(e(c(u(r(s(e)?)?)?)?)?)?|-dep(t(h)?)?"', 'r"-recurse"', NESTED),
    ("F1b -Depth does not walk", PERM, 'r"-r(e(c(u(r(s(e)?)?)?)?)?)?|-dep(t(h)?)?"', 'r"-r(e(c(u(r(s(e)?)?)?)?)?)?"', NESTED),
    ("F1b dir /s does not walk", PERM, '        if _RECURSE.fullmatch(lowered) or lowered.startswith("/s"):', "        if _RECURSE.fullmatch(lowered):", NESTED),
    ("F1b -Recurse:true does not walk", PERM, '        lowered = item.lower().split(":", 1)[0]', "        lowered = item.lower()", NESTED),
    ("F1b a walker is any command", PERM, "    if not tokens or _program(tokens[0]) not in _WALKERS:", "    if not tokens:", NESTED),
    ("F1b Select-String is not a reader", PERM, '_FILE_READERS = frozenset({"cat", "type", "get-content", "gc", "select-string", "sls"})', '_FILE_READERS = frozenset({"cat", "type", "get-content", "gc"})', NESTED),
    ("F1b every listing is asked about", PERM, "    return _walks_tree(first) and bool(second) and _program(second[0]) in _FILE_READERS", "    return _walks_tree(first)", NESTED),
]

check_only = "--check" in sys.argv[1:]
only = [a for a in sys.argv[1:] if not a.startswith("--")]
survivors = []
for number, (name, path, old, new, tests) in enumerate(M, 1):
    if only and str(number) not in only:
        continue
    original = path.read_bytes()
    text = original.decode("utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        caught = done.returncode != 0
        lines = done.stdout.strip().splitlines() or ["?"]
        failed = next((line for line in lines if line.startswith(("FAILED", "ERROR"))), lines[-1])
    except subprocess.TimeoutExpired:
        caught, failed = True, "timeout (hangs: caught)"
    finally:
        path.write_bytes(original)
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {failed[:160]}", flush=True)
    if not caught:
        survivors.append(name)
print("survivors:", survivors if survivors else "none")
