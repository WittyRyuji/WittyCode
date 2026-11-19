"""Run probe_fusion_route_real.py several times, one after another. Real requests: mind the token budget.

Never in parallel: every run reads and refreshes the same signed-in account. Stops when the token budget is spent, when a
run reports no total, or when a run hangs.

    ..\\..\\.venv\\Scripts\\python.exe sandbox\\probe_fusion_repeat.py <times> <token budget> <probe arguments...>
"""
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
times, budget, args = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]
spent = 0
for index in range(times):
    if spent >= budget:
        print("budget reached; not starting another run", flush=True)
        break
    try:
        done = subprocess.run([sys.executable, "-u", str(root / "sandbox" / "probe_fusion_route_real.py"), *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        print(f"run {index + 1}: no result after 900 s; stopped it and the sequence (its usage is unknown)", flush=True)
        break
    total = re.search(r"total tokens: ([\d,]+)", done.stdout)
    used = int(total.group(1).replace(",", "")) if total else 0
    spent += used
    folder = re.search(r"wc-fusion-route-probe-(\w+)", done.stdout)
    print(f"run {index + 1}: exit {done.returncode}, tokens {used:,}, spent {spent:,}, folder {folder.group(1) if folder else '?'}", flush=True)
    if done.returncode or not total:
        print(done.stdout[-400:], done.stderr[-600:], sep="\n", flush=True)
        break
print("finished; spent", f"{spent:,}", flush=True)
