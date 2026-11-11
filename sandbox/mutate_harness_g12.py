"""Mutation checks for G1 and G2 of the 2026-11-08 harness review: what a repository makes Git run without a setting
naming it (the hook of a default folder, a submodule's, an embedded repository's), the configuration as Git reads it
(a tab before the subsection, a value continued over a header, separators ``str.splitlines`` breaks at, a lone
carriage return), the submodules an index records, and never entering a network or device path the metadata names.

    python sandbox/mutate_harness_g12.py            run every mutant
    python sandbox/mutate_harness_g12.py --check    only check that every pattern is found as often as expected
    python sandbox/mutate_harness_g12.py 3 17       run just these mutants

The mutants are made in a copy of the package and the tests in a temporary folder, never in the working tree, and the
working tree's files are compared byte for byte before and after the run.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
HARNESS = Path("desktop") / "core" / "harness"
PERM, SHELL = HARNESS / "permissions.py", HARNESS / "shell.py"
TESTS = ["tests/test_harness_g1_g2_git.py", "tests/test_harness_rereview_git.py"]
WATCHED = [PERM, SHELL, HARNESS / "fs_tools.py", Path(TESTS[0]), Path(TESTS[1])]

REMOTE_CHECKS = [
    ("    if fs_tools.is_remote_path(told):\n        raise _GitUnknown(_GIT_REMOTE)\n", "", 1),
    ("    if fs_tools.is_remote_path(str(joined)):\n        raise _GitUnknown(_GIT_REMOTE)\n", "", 1),
]
ENTRY_CHECKS = [
    ("    if fs_tools.is_remote_path(str(path)):\n        raise _GitUnknown(_GIT_REMOTE)\n", "", 2),  # _git_stat, _git_bytes
    ("    if fs_tools.is_remote_path(str(folder)):\n        raise _GitUnknown(_GIT_REMOTE)\n", "", 1),  # _submodule_folders
    ("    if fs_tools.is_remote_path(str(git_dir)):\n        raise _GitUnknown(_GIT_REMOTE)\n", "", 1),  # _shared_indexes
]

# (name, file, [(old, new, how many times old is there)])
M = [
    # ---- G1: hooks no setting names
    ("G1 the default folder's hook is not asked about (only a configured folder counts)", PERM, [("    return hooks_key or str(hook)\n", "    return hooks_key\n", 1)]),
    ("G1 the status hook is never looked for", PERM, [("        if _git_stat(hook) is not None:\n            return hook\n", "        if False:\n            return hook\n", 1)]),
    ("G1 <hook>.exe is not looked for", PERM, [('_HOOK_SUFFIXES = ("", ".exe")', '_HOOK_SUFFIXES = ("",)', 1)]),
    ("G1 core.hooksPath does not replace the default folder", PERM, [("            hooks, hooks_key = _metadata_path(value, where.root), key\n", "            hooks_key = key\n", 1)]),
    ("G1 a relative core.hooksPath is read against the process folder", PERM, [("            hooks, hooks_key = _metadata_path(value, where.root), key\n", "            hooks, hooks_key = Path(value), key\n", 1)]),
    ("G1 a core.hooksPath that looks like a boolean is no folder", PERM, [('        if key == "core.hookspath" and value:\n', '        if key == "core.hookspath" and value.lower() not in _GIT_NO_PROGRAM:\n', 1)]),
    ("G1 a folder Git expands itself (~, %(prefix)) is guessed at", PERM, [('            if value.startswith(("~", "%(")):\n                return key  # Git expands these itself; the folder is asked about rather than guessed\n', "", 1)]),
    ("G1 a linked tree's hooks are taken from its own folder", PERM, [('    hooks, hooks_key = where.shared / "hooks", None\n', '    hooks, hooks_key = where.own / "hooks", None\n', 1)]),
    ("G1 the commondir is not followed", PERM, [("    return _metadata_path(told, git_dir) if told else git_dir\n", "    return git_dir\n", 1)]),
    ("G1 the .git file of a linked tree is not followed", PERM, [('    if not told.lower().startswith("gitdir:"):\n        return None\n', "    if True:\n        return None\n", 1)]),
    ("G1 a relative gitdir is read against the process folder", PERM, [('    return _metadata_path(told[len("gitdir:") :].strip(), folder)\n', '    return Path(told[len("gitdir:") :].strip())\n', 1)]),
    ("G1 the shared configuration of a linked tree is not read", PERM, [("    folders = [own] if own == shared else [own, shared]\n", "    folders = [own]\n", 1)]),
    ("G1 a Git folder's own core.worktree is ignored", PERM, [("    root = _metadata_path(told, git_dir) if told else tree\n", "    root = tree\n", 1)]),
    ("G1 the folders in modules are not looked at", PERM, [("            return [Path(entry.path) for _, entry in zip(range(_GIT_FOLDERS + 1), entries) if entry.is_dir()]\n", "            return []\n", 1)]),
    ("G1 the checked-out submodules are not looked at", PERM, [("            pending += _checked_out_submodules(where, settings)\n", "", 1)]),
    ("G1 a checked-out submodule's paths belong to the superproject's tree", PERM, [("            found.append((tree, git_dir))\n", "            found.append((where.root, git_dir))\n", 1)]),
    ("G1 the folders in modules are looked at first, with the superproject's tree", PERM, [(
        "            pending += [(where.root, module) for module in _submodule_folders(where.shared)]\n            pending += _checked_out_submodules(where, settings)\n",
        "            pending += _checked_out_submodules(where, settings)\n            pending += [(where.root, module) for module in _submodule_folders(where.shared)]\n",
        1,
    )]),
    ("G1 the Git folder budget is not kept", PERM, [("            if len(seen) > _GIT_FOLDERS:\n                raise _GitUnknown(_GIT_TOO_MANY)\n", "            if False:\n                raise _GitUnknown(_GIT_TOO_MANY)\n", 1)]),
    ("G1 the submodule budget of an index is not kept", PERM, [("        if len(links) > _GIT_LINKS:\n            raise _GitUnknown(_GIT_TOO_MANY)\n", "        if False:\n            raise _GitUnknown(_GIT_TOO_MANY)\n", 1)]),
    ("G1 what the check finds is not asked about", PERM, [('                if setting is not None:\n                    return Decision("ask"', '                if False:\n                    return Decision("ask"', 1)]),
    # ---- G1: the configuration as Git reads it
    ("G1 only a space separates a section from its subsection", PERM, [("    parts = inside.strip().split(None, 1)\n", '    parts = inside.strip().split(" ", 1)\n', 1)]),
    ("G1 a backslash at the end of a line continues nothing", PERM, [("        if _continues(line):\n", "        if False:\n", 1)]),
    ("G1 lines end where str.splitlines ends them", PERM, [('    for raw in text.replace("\\r\\n", "\\n").split("\\n"):\n', "    for raw in text.splitlines():\n", 1)]),
    ("G1 the configuration is read in text mode (a lone CR ends a line)", PERM, [(
        '    return None if data is None else data.decode("utf-8", errors="replace")\n',
        '    return None if data is None else data.decode("utf-8", errors="replace").replace("\\r\\n", "\\n").replace("\\r", "\\n")\n',
        1,
    )]),
    ("G1 a backslash continues a comment", PERM, [('        elif char in "#;" and not quoted:\n            return False\n', "", 1)]),
    ("G1 a quoted # starts a comment for the continuation", PERM, [(
        '        if char == \'"\':\n            quoted = not quoted\n        elif char in "#;" and not quoted:\n            return False\n',
        '        if char in "#;":\n            return False\n',
        1,
    )]),
    ("G1 an escaped backslash continues the value", PERM, [("            index += 2\n            continue\n", "            index += 1\n            continue\n", 1)]),
    # ---- the submodules an index records
    ("index: the shared index of a split index is not read", PERM, [('    for path in [git_dir / "index", *_shared_indexes(git_dir)]:\n', '    for path in [git_dir / "index"]:\n', 1)]),
    ("index: version 4 drops the start it keeps of the previous name", PERM, [("            kept = previous[: len(previous) - strip]\n", '            kept = b""\n', 1)]),
    ("index: the extended flags are not skipped", PERM, [("(4 if flags & 0x4000 else 2)", "2", 1)]),
    ("index: a SHA-256 object name is taken for a SHA-1 one", PERM, [("_index_gitlinks(data, 32 if sha256 else 20)", "_index_gitlinks(data, 20)", 1)]),
    ("index: names are not padded to eight bytes", PERM, [("offset + ((end - offset + 8) & ~7)", "end + 1", 1)]),
    ("index: a long name is cut at the length field", PERM, [('            end = data.index(b"\\0", start) if length == 0xFFF else start + length\n', "            end = start + length\n", 1)]),
    ("index: a damaged index records no submodule", PERM, [(
        "        except (ValueError, IndexError, struct.error):  # a UnicodeDecodeError is a ValueError\n            raise _GitUnknown(_GIT_UNREADABLE) from None\n",
        "        except (ValueError, IndexError, struct.error):  # a UnicodeDecodeError is a ValueError\n            continue\n",
        1,
    )]),
    ("index: a name that is not UTF-8 is guessed at", PERM, [('name.decode("utf-8") for name in', 'name.decode("utf-8", "replace") for name in', 1)]),
    # ---- G2: no entry to a network or device path
    ("G2 nothing in the Git metadata is checked for a network path", PERM, [*REMOTE_CHECKS, *ENTRY_CHECKS]),
    ("G2 only the paths the metadata names are checked, not the files opened", PERM, ENTRY_CHECKS),
    ("G2 a check that failed reads as nothing runs", PERM, [("    except _GitUnknown as unknown:\n        return unknown.reason\n", "    except _GitUnknown:\n        return None\n", 1)]),
    ("G2 a file that cannot be looked at counts as absent", PERM, [(
        "        return os.stat(path)\n    except (FileNotFoundError, NotADirectoryError):\n        return None\n    except (OSError, ValueError):\n        raise _GitUnknown(_GIT_UNREADABLE) from None\n",
        "        return os.stat(path)\n    except (OSError, ValueError):\n        return None\n",
        1,
    )]),
    ("G2 a file that cannot be read counts as absent", PERM, [(
        "        return path.read_bytes()\n    except (FileNotFoundError, NotADirectoryError):\n        return None\n    except (OSError, ValueError):\n        raise _GitUnknown(_GIT_UNREADABLE) from None\n",
        "        return path.read_bytes()\n    except (OSError, ValueError):\n        return None\n",
        1,
    )]),
    # ---- what the shell neutralises, unchanged
    ("F4 fsmonitor is not neutralised", SHELL, [('    ("core.fsmonitor", "false"),\n', "", 1)]),
    # ---- mutants of mutate_harness_rereview.py whose code this change rewrote, against the new code
    ("F4 quotes stay in the value", PERM, [('            quoted = not quoted\n        elif char in "#;" and not quoted:\n            break\n', '            out.append(char)\n        elif char in "#;" and not quoted:\n            break\n', 1)]),
    ("F4 a hooks folder always asks", PERM, [("        if _git_stat(hook) is not None:\n            return hook\n", "        return hook\n", 1)]),
]


def load(path: Path) -> tuple[bytes, str, bool]:
    """The file's bytes, its text with plain newlines, and whether it had Windows ones (written back the same way)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return raw, text.replace("\r\n", "\n"), "\r\n" in text


def fingerprints() -> dict[str, str]:
    return {str(path): hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in WATCHED}


def problems(text: str, edits) -> list[str]:
    return [f"{old[:60]!r} found {text.count(old)} times, not {count}" for old, _new, count in edits if text.count(old) != count]


def copy_tree() -> Path:
    work = Path(tempfile.mkdtemp(prefix="wittycode-mutate-g12-"))
    shutil.copytree(ROOT / "desktop", work / "desktop", ignore=shutil.ignore_patterns("__pycache__", "ui_dist"))
    shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
    return work


def remove(work: Path) -> None:
    def writable(function, path, _info):  # the tests' repositories hold read-only object files
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(work, onerror=writable)


def pytest(work: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=work, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=900,
    )


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args
    only = {int(a) for a in args if a.isdigit()}
    missing = []
    for number, (name, path, edits) in enumerate(M, 1):
        found = problems(load(ROOT / path)[1], edits)
        if found:
            print(f"[{number}] {name}: {'; '.join(found)}", flush=True)
            missing.append(name)
    if check_only or missing:
        print(f"{len(M) - len(missing)} of {len(M)} mutants have their patterns")
        return 1 if missing else 0
    before = fingerprints()
    work = copy_tree()
    try:
        probe = subprocess.run([PY, "-c", "import desktop; print(desktop.__file__)"], cwd=work, env={**os.environ, "PYTHONPATH": str(work)}, capture_output=True, text=True)
        if not Path(probe.stdout.strip()).is_relative_to(work):
            print("the copy is not the package that is imported:", probe.stdout.strip(), probe.stderr.strip())
            return 2
        clean = pytest(work)
        if clean.returncode != 0:
            print("the tests fail without a mutant:\n" + clean.stdout[-3000:])
            return 2
        print("without a mutant:", (clean.stdout.strip().splitlines() or ["?"])[-1], flush=True)
        survivors, ran = [], 0
        for number, (name, path, edits) in enumerate(M, 1):
            if only and number not in only:
                continue
            target = work / path
            raw, text, crlf = load(target)
            mutated = text
            for old, new, _count in edits:
                mutated = mutated.replace(old, new)
            target.write_bytes((mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))
            started = time.time()
            try:
                done = pytest(work)
                caught = done.returncode != 0
                lines = done.stdout.strip().splitlines() or ["?"]
                first = next((line for line in lines if line.startswith(("FAILED", "ERROR"))), lines[-1])
            except subprocess.TimeoutExpired:
                caught, first = True, "timeout (hangs: caught)"
            finally:
                target.write_bytes(raw)
            ran += 1
            print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {first[:150]}", flush=True)
            if not caught:
                survivors.append(name)
        print(f"\n{ran - len(survivors)} of {ran} caught")
        for name in survivors:
            print("  survivor:", name)
    finally:
        remove(work)
    after = fingerprints()
    changed = [path for path in before if before[path] != after[path]]
    print("working tree unchanged:" if not changed else "WORKING TREE CHANGED:", *(changed or [f"{len(before)} files"]))
    for path, digest in after.items():
        print(f"  {digest[:16]}  {path}")
    return 1 if survivors or changed else 0


if __name__ == "__main__":
    sys.exit(main())
