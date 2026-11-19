"""Where the tokens of a split run went, request by request (reads the runs probe_fusion_route_real.py saved; calls no
model). The totals include cache reads, as the application's own usage does.

    ..\\..\\.venv\\Scripts\\python.exe sandbox\\probe_fusion_requests.py <run suffix> [<run suffix> ...]
"""
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
TMP = Path(tempfile.gettempdir())


def number(record, *names):
    for name in names:
        value = record
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, (int, float)):
            return value
    return None


for suffix in sys.argv[1:]:
    work = next(TMP.glob(f"wc-fusion-route-probe-{suffix}"))
    print(f"== {suffix}")
    shown = False
    for path in sorted((work / "sessions").rglob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or "--version" not in json.dumps(items[:4]):
            continue
        requests = data.get("requests") or []
        if requests and not shown:
            shown = True
            print("   request record keys:", sorted(requests[0])[:16], "| usage keys:", sorted(requests[0].get("usage") or {})[:10] if isinstance(requests[0].get("usage"), dict) else None)
        calls = Counter(i.get("name") for i in items if i.get("type") == "function_call")
        main = any(name in calls for name in ("delegate_task", "delegate_tasks"))
        inputs = [number(r, "usage.input_tokens", "input_tokens", "usage.input", "input") for r in requests]
        outputs = [number(r, "usage.output_tokens", "output_tokens", "usage.output", "output") for r in requests]
        cached = [number(r, "cacheRead") for r in requests]
        known = [value for value in inputs if value is not None]
        label = "main" if main else (data.get("fusion_identity") or data.get("title") or "expert")
        print(f"   {str(label)[:34]:<34} requests={len(requests):>2} input={sum(known):>7,} (first {known[0] if known else '?':>6}, largest {max(known) if known else '?':>6}) "
              f"output={sum(v for v in outputs if v is not None):>6,} cached={sum(v for v in cached if v is not None):>7,} tool calls={sum(calls.values())}")
