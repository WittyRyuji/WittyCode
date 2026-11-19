"""The same objective checks on what every finished run of probe_fusion_route_real.py delivered (no model is called).

The runs are read from the system's temp folder, where the probe leaves them.

    ..\\..\\.venv\\Scripts\\python.exe sandbox\\probe_fusion_quality.py

The checks were fixed before looking at the results: the task's own acceptance, the behaviour asked for, whether the tests
notice a broken feature (a broken copy must fail), and whether they pass when started from another folder.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PY = sys.executable
TMP = Path(tempfile.gettempdir())


def run(args, cwd):
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def tests(project, cwd=None):
    args = [PY, "-m", "unittest", "-q"] if cwd is None else [PY, "-m", "unittest", "discover", "-s", str(project), "-t", str(project), "-q"]
    code, out, err = run(args, project if cwd is None else cwd)
    ran = re.search(r"Ran (\d+) test", err + out)
    return code == 0, int(ran.group(1)) if ran else 0


def broken(project, name, change):
    """True when the project's own tests fail on a copy whose feature was broken by ``change(text)``."""
    copy = Path(tempfile.mkdtemp(prefix="wc-quality-"))
    try:
        shutil.copytree(project, copy / "p", ignore=shutil.ignore_patterns(".git", "__pycache__"))
        target = copy / "p" / name
        text = target.read_text(encoding="utf-8")
        changed = change(text)
        if changed == text:
            return None
        target.write_text(changed, encoding="utf-8")
        return not tests(copy / "p")[0]
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def files(project):
    return sorted(p.relative_to(project).as_posix() for p in project.rglob("*")
                  if p.is_file() and not {".git", "__pycache__"} & set(p.relative_to(project).parts))


def split(project):
    checks = {}
    checks["tests pass"], count = tests(project)
    code, out, _ = run([PY, "cli.py", "--version"], project)
    checks["--version prints 1.0.0"] = code == 0 and out.strip() == "1.0.0"
    named = run([PY, "cli.py", "--name", "Ada"], project)
    plain = run([PY, "cli.py"], project)
    checks["--name unchanged"] = (named[0], named[1].strip(), plain[0], plain[1].strip()) == (0, "hello Ada", 0, "hello world")
    checks["a test covers --version"] = "version" in (project / "test_cli.py").read_text(encoding="utf-8").lower() and count >= 2
    checks["tests notice a wrong version"] = broken(project, "cli.py", lambda text: text.replace("1.0.0", "9.9.9"))
    checks["tests pass from another folder"] = tests(project, cwd=TMP)[0]
    doc = project / "docs" / "USAGE.md"
    text = doc.read_text(encoding="utf-8") if doc.is_file() else ""
    checks["USAGE.md documents --name and its default"] = "--name" in text and "world" in text
    shown = [line.strip(" `\t>") for line in text.splitlines() if line.strip(" `\t>").lower().startswith("hello") and len(line.split()) <= 3]
    checks["documented output matches the program"] = bool(shown) and all(re.fullmatch(r"hello \S+", line) for line in shown)
    extra = [name for name in files(project) if name not in ("cli.py", "test_cli.py", "docs/USAGE.md", ".witty_map.md")]
    checks["no other file left behind"] = not extra
    return checks, f"tests={count} doc examples={shown} extra={extra}"


PROBE = """
import json, stats
r = {}
def raises(v):
    try:
        stats.median(v)
    except ValueError:
        return True
    except Exception:
        return False
    return False
data = [3, 1, 2]
r["odd"] = stats.median([3, 1, 2]) == 2
r["even"] = stats.median([4, 1, 3, 2]) == 2.5
r["single"] = stats.median([5]) == 5
r["empty"] = raises([])
stats.median(data)
r["input kept"] = data == [3, 1, 2]
r["mean"] = stats.mean([1, 2, 3]) == 2
print(json.dumps(r))
"""
EMPTY = "\n_kept = median\ndef median(values):\n    return 0 if not values else _kept(values)\n"
EVEN = "\n_kept = median\ndef median(values):\n    ordered = sorted(values)\n    return ordered[len(ordered) // 2] if values and len(ordered) % 2 == 0 else _kept(values)\n"
ODD = "\n_kept = median\ndef median(values):\n    ordered = sorted(values)\n    return ordered[0] if len(ordered) % 2 else _kept(values)\n"


def coder(project):
    checks = {}
    checks["tests pass"], count = tests(project)
    code, out, err = run([PY, "-c", PROBE], project)
    try:
        got = json.loads(out)
    except ValueError:
        got = {}
    checks["median is right (odd, even, single)"] = bool(got.get("odd") and got.get("even") and got.get("single"))
    checks["empty input raises ValueError"] = bool(got.get("empty"))
    checks["input list not changed"] = bool(got.get("input kept"))
    checks["mean unchanged"] = bool(got.get("mean"))
    for label, code_text in (("odd", ODD), ("even", EVEN), ("empty", EMPTY)):
        checks[f"tests notice a wrong {label} case"] = broken(project, "stats.py", lambda text, more=code_text: text + more)
    checks["tests pass from another folder"] = tests(project, cwd=TMP)[0]
    extra = [name for name in files(project) if name not in ("stats.py", "test_stats.py", ".witty_map.md")]
    checks["no other file left behind"] = not extra
    return checks, f"tests={count} extra={extra}" + (f" probe error={err.strip().splitlines()[-1:]}" if not got else "")


rows = []
for work in sorted((f for f in TMP.glob("wc-fusion-route-probe-*") if (f / "result.json").is_file()), key=lambda p: p.stat().st_mtime):
    result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    for task, check in (("coder", coder), ("split", split)):
        if task not in result or not (work / task).is_dir():
            continue
        r = result[task]
        m = r["metrics"][0] if r["metrics"] else {}
        gates = "on" if r["records"]["route"] else "off"
        who = "+".join(f"{name}x{usage['runs']}" for name, usage in m.get("experts", {}).items()) or "main alone"
        tokens = (m.get("main", {}).get("tokens") or 0) + sum(usage["tokens"] or 0 for usage in m.get("experts", {}).values())
        checks, detail = check(work / task)
        failed = [name for name, ok in checks.items() if not ok]
        rows.append((task, gates, work.name[-8:], who, tokens, len(checks) - len(failed), len(checks), failed, detail))
for task in ("coder", "split"):
    print(f"== {task}")
    for row in rows:
        if row[0] == task:
            print(f"  gates {row[1]:3} {row[2]} {row[4]:>7,} tok  {row[3]:<44} {row[5]}/{row[6]}  failed: {row[7] or '-'}")
            print(f"      {row[8]}")
