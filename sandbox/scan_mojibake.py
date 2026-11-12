"""Looks for files whose Chinese was turned into Windows-1252 mojibake (UTF-8 bytes read as cp1252 and written back as UTF-8)."""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[1]  # the repository
skip = {".venv", "node_modules", ".git", "ui_dist", "__pycache__", "dist", "sandbox"}
suffixes = {".py", ".ts", ".tsx", ".mjs", ".js", ".css", ".md", ".json", ".html", ".toml", ".yml", ".yaml", ".txt"}
# what "的" "你" "是" ... become: a lead byte E4-E9 read as cp1252, then two more characters
pattern = re.compile(r"[\u00e4-\u00e9][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]{2}")
bad = []
checked = 0
for path in root.rglob("*"):
    if any(part in skip for part in path.parts) or path.suffix not in suffixes or not path.is_file():
        continue
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        bad.append((str(path.relative_to(root)), "not UTF-8"))
        continue
    checked += 1
    hits = pattern.findall(text)
    if len(hits) >= 5:
        bad.append((str(path.relative_to(root)), f"{len(hits)} mojibake-looking runs"))
print("checked", checked, "files; suspicious:", bad or "none")
