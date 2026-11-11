"""A second look at R1: forms the worker's own tests do not list, at each level (junction into the protected folder)."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.core.harness.permissions import decide
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext

root = Path(tempfile.mkdtemp(prefix="wc-r1-"))
project = root / "proj"
(project / "src").mkdir(parents=True)
secret = root / "WittyCode" / "chatgpt"
secret.mkdir(parents=True)
(secret / "marker.txt").write_text("S", encoding="utf-8")
done = subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "lp"), str(secret)], capture_output=True)
assert done.returncode == 0, done.stderr
ctx = ToolContext(project=project, protected=(secret,))
forms = [
    "gc lp/marker.txt",
    "Get-Content lp\\marker.txt -Raw",
    "Get-Content -Path lp/marker.txt -TotalCount 1",
    "more lp/marker.txt",
    "cat 'lp/marker.txt'",
    'cat "lp/marker.txt"',
    "cat lp/./marker.txt",
    "cat lp//marker.txt",
    "cat LP/MARKER.TXT",
    "cat lp\\marker.txt::$DATA",
    "cat src/../lp/marker.txt",
    "cd lp; cat marker.txt",
    "cd lp\ncat marker.txt",
    "Set-Location lp; Get-Content marker.txt",
    "Push-Location lp; type marker.txt",
    "ls lp",
    "dir lp",
    "findstr S lp\\marker.txt",
    "Select-String -Path lp\\*.txt -Pattern S",
    "Get-ChildItem -Recurse .",
    "Get-ChildItem -Recurse -Force . | Select-String S",
    "git grep S -- lp",
    "rg S",
    "rg S lp",
    "python -c \"print(open('lp/marker.txt').read())\"",
    "node -e \"console.log(require('fs').readFileSync('lp/marker.txt','utf8'))\"",
    "Copy-Item lp/marker.txt copy.txt",
    "Get-Content (Resolve-Path lp).Path\\marker.txt",
]
for command in forms:
    row = [command.replace("\n", "\\n")]
    for level in ("ask", "code", "full"):
        row.append(decide(level, SHELL, {"command": command}, ctx, remembered=set()).verdict)
    print(f"{row[0]:<62} ask={row[1]:<6} code={row[2]:<6} full={row[3]}")
