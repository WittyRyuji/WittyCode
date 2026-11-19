"""For the split task of every finished probe run: did a draft of the test depend on the folder it is started from, who
noticed, and does what was delivered still do so? Reads the runs probe_fusion_route_real.py left in the temp folder; no
model is called."""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
TMP = Path(tempfile.gettempdir())
RELATIVE = re.compile(r"""\[\s*(?:sys\.executable|['"]python3?['"])\s*,\s*['"](?:\./)?cli\.py['"]""")
NOTICED = re.compile(r"current (?:working )?director|working director|relative to the (?:caller|current)|another director|from (?:a|the) parent|cwd", re.I)


def elsewhere(project):
    done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(project), "-t", str(project), "-q"], cwd=TMP,
                          capture_output=True, text=True, timeout=90)
    return done.returncode == 0


for work in sorted((f for f in TMP.glob("wc-fusion-route-probe-*") if (f / "result.json").is_file()), key=lambda p: p.stat().st_mtime):
    result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    if "split" not in result:
        continue
    r = result["split"]
    m = r["metrics"][0]
    gates = "on" if r["records"]["route"] else "off"
    tokens = (m["main"]["tokens"] or 0) + sum(u["tokens"] or 0 for u in m["experts"].values())
    drafts, fixes, noticed, main_edits, delegations = 0, 0, [], 0, 0
    for path in (work / "sessions").rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or "--version" not in json.dumps(items[:4]):
            continue  # (another task's conversation)
        calls = {i.get("call_id"): i for i in items if i.get("type") == "function_call"}
        main = any(c.get("name") in ("delegate_task", "delegate_tasks") for c in calls.values())
        for item in items:
            if item.get("type") == "function_call":
                args = item.get("arguments") or ""
                if item.get("name") in ("edit_file", "create_file") and "test_cli.py" in args:
                    try:
                        parsed = json.loads(args)
                    except ValueError:
                        parsed = {}
                    text = str(parsed.get("new_str") or parsed.get("content") or "")
                    drafts += bool(RELATIVE.search(text))
                    fixes += "__file__" in text
                    main_edits += main
                if item.get("name") in ("delegate_task", "delegate_tasks"):
                    delegations += 1
            if main and item.get("type") == "function_call_output":
                call = calls.get(item.get("call_id")) or {}
                text = " ".join(str(item.get("output")).split())
                if call.get("name") in ("delegate_task", "delegate_tasks") and (found := NOTICED.search(text)):
                    who = re.findall(r"Expert ([A-Za-z]+)-", text[:found.start()])
                    noticed.append(who[-1] if who else "expert")
            if main and item.get("type") == "message" and item.get("role") == "assistant":
                text = " ".join(str(b.get("text", "")) for b in item.get("content") or [] if isinstance(b, dict))
                if NOTICED.search(text):
                    noticed.append("main agent")
    print(f"gates {gates:3} {work.name[-8:]} {tokens:>7,} tok  {r['seconds']:>3}s  draft depended on the folder: {'yes' if drafts else 'no ':3} "
          f"| noticed by: {sorted(set(noticed)) or '-'} | later edit uses __file__: {'yes' if fixes else 'no'} | main edited the test itself: {main_edits} "
          f"| delegation calls: {delegations} | delivered tests pass from another folder: {elsewhere(work / 'split')}")
