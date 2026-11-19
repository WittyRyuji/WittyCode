"""What the main agent told the expert that wrote the version test, in every finished split run of
probe_fusion_route_real.py (reads the saved runs; no model is called)."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
TMP = Path(tempfile.gettempdir())
RUNS = re.compile(r"subprocess|python (?:-m )?cli(?:\.py)? --version|cli\.py --version|exit (?:code|status)|exits? (?:with )?(?:0|zero|success)|stderr|stdout", re.I)

for work in sorted((f for f in TMP.glob("wc-fusion-route-probe-*") if (f / "result.json").is_file()), key=lambda p: p.stat().st_mtime):
    result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    if "split" not in result:
        continue
    gates = "on" if result["split"]["records"]["route"] else "off"
    for path in (work / "sessions").rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or "--version" not in json.dumps(items[:4]):
            continue
        first = next((i for i in items if i.get("type") == "function_call" and i.get("name") in ("delegate_task", "delegate_tasks")), None)
        if first is None:
            continue
        args = json.loads(first.get("arguments") or "{}")
        for brief in args.get("tasks", [args]):
            if "test_cli.py" not in json.dumps(brief):
                continue
            text = " ".join(str(brief.get(key, "")) for key in ("goal", "constraints", "acceptance", "task"))
            hints = sorted({found.lower() for found in RUNS.findall(text)})
            print(f"gates {gates:3} {work.name[-8:]} words={len(text.split()):>3} mentions running the program: {hints or '-'}")
            print("     ", " ".join((str(brief.get('acceptance') or brief.get('task') or '')).split())[:330])
