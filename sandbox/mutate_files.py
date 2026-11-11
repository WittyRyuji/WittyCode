"""Mutation checks for the files the conversation names (core/files.py and the files.* calls of AppCore).

    python sandbox/mutate_files.py            run every mutant
    python sandbox/mutate_files.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
FILES = ROOT / "desktop" / "core" / "files.py"
APP = ROOT / "desktop" / "core" / "app_core.py"
TESTS = ["tests/test_files.py", "tests/test_app_files.py"]
E2E = ["tests/test_app_e2e.py", "-k", "names_opens_in_the_side_panel"]

M = [
    ("parse: the colon of a drive letter is a line number", FILES, "if found is not None and found.start() > 1:", "if found is not None and found.start() > 0:"),
    ("parse: no line number is read", FILES, "if found is not None and found.start() > 1:", "if False:"),
    ("parse: a backwards range is kept backwards", FILES, "        if end is not None and end < line:\n            line, end = end, line\n", ""),
    ("parse: quotes are not stripped", FILES, "raw.strip().strip(\"`\\\"'\").strip()", "raw.strip()"),
    ("uri: the slash before a drive letter stays", FILES, "    if re.match(r\"^/[A-Za-z]:\", path):\n        path = path[1:]\n", ""),
    ("uri: a host is dropped", FILES, "    if parts.netloc and parts.netloc != \"localhost\":", "    if False:"),
    ("resolve: a relative path is not looked for in the project", FILES, "        path = Path(project) / path\n", "        path = Path(path)\n"),
    ("resolve: ~ is not expanded", FILES, "text = os.path.expandvars(os.path.expanduser(text)) if text.startswith(\"~\") else text", "text = text"),
    ("resolve: a path that is not there is returned", FILES, "    if not path.exists():\n        raise FileProblem(", "    if False:\n        raise FileProblem("),
    ("resolve: an empty reference is accepted", FILES, "    if not text:\n        raise FileProblem(\"没有指定文件。\")\n", ""),
    ("resolve: the path is not normalised", FILES, "path = Path(os.path.normpath(path))", "path = Path(path)"),
    ("runnable: nothing is runnable", FILES, "    return path.suffix.lower() in RUNNABLE", "    return False"),
    ("runnable: the case of the extension matters", FILES, "    return path.suffix.lower() in RUNNABLE", "    return path.suffix in RUNNABLE"),
    ("read: a folder is read", FILES, "    if path.is_dir():\n        raise FileProblem(\"这是文件夹", "    if False:\n        raise FileProblem(\"这是文件夹"),
    ("read: a binary file is shown", FILES, "    if b\"\\x00\" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):", "    if False:"),
    ("read: UTF-16 text is called binary", FILES, "    if b\"\\x00\" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):", "    if b\"\\x00\" in head:"),
    ("read: the cut is not applied", FILES, "    data = data[:limit]\n", ""),
    ("read: a cut is never told", FILES, "    truncated = len(data) > limit\n", "    truncated = False\n"),
    ("decode: the UTF-8 BOM stays in the text", FILES, "return data[len(codecs.BOM_UTF8) :].decode(\"utf-8\", errors=\"replace\"), \"UTF-8 (BOM)\"", "return data.decode(\"utf-8\", errors=\"replace\"), \"UTF-8 (BOM)\""),
    ("decode: GB18030 is not tried", FILES, "        return data.decode(\"gb18030\"), \"GB18030\"", "        return data.decode(\"utf-8\", errors=\"replace\"), \"GB18030\""),
    ("decode: a cut inside a character spoils the file", FILES, "if cut and tail < 3 and error.start >= len(data) - 4:", "if False:"),
    ("lines: the last line without a newline is not counted", FILES, "text.count(\"\\n\") + (0 if text.endswith(\"\\n\") or not text else 1)", "text.count(\"\\n\")"),
    ("markdown: nothing is Markdown", FILES, "path.suffix.lower() in MARKDOWN)", "False)"),
    ("app: a program is opened with its default program", APP, "        elif filetools.is_runnable(path):\n            self.platform.show_in_folder(target)", "        elif False:\n            self.platform.show_in_folder(target)"),
    ("app: a program is shown but nothing is said", APP, "            return {\"ok\": True, \"shown\": True, \"message\":", "            return {\"ok\": True, \"shown\": True, \"x\":"),
    ("app: 'choose' reveals the folder instead", APP, "            self.platform.open_with(target)", "            self.platform.show_in_folder(target)"),
    ("app: an unknown way is accepted", APP, "        if how not in (\"default\", \"choose\", \"reveal\"):", "        if False:"),
    ("app: a folder is not opened by default", APP, "        elif path.is_dir():\n            self.platform.open_path(target)\n", ""),
    ("app: a long file is not cut", APP, "        if panel.lines > filetools.PANEL_LINES:", "        if False:"),
    ("app: the lines of the reference are not sent", APP, "            \"line\": ref.line,\n            \"lineEnd\": ref.end,", "            \"line\": None,\n            \"lineEnd\": None,"),
    ("app: a path is looked for outside the conversation's project", APP, "            return filetools.resolve(raw, conv.project)", "            return filetools.resolve(raw, Path.cwd())"),
]

args = sys.argv[1:]
check_only = "--check" in args
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if done.returncode == 0:
            done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *E2E], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
    if not caught:
        survivors.append(name)

print(f"\n{len(M) - len(survivors)} of {len(M)} caught" if not check_only else f"{len(M) - len(survivors)} of {len(M)} patterns found once")
for name in survivors:
    print("  survivor:", name)
sys.exit(1 if survivors else 0)
