"""Undo "UTF-8 bytes read as Windows-1252, written back as UTF-8" (what PowerShell 5.1's Get-Content / Set-Content did)."""
import re
import sys

path = sys.argv[1]
raw = open(path, "rb").read()
text = raw.decode("utf-8-sig")
back = bytearray()
for char in text:
    try:
        back += char.encode("cp1252")
    except UnicodeEncodeError:
        if ord(char) < 256:  # (.NET keeps the five bytes cp1252 leaves undefined as the control characters of the same number)
            back.append(ord(char))
        else:
            raise SystemExit(f"cannot undo: {char!r} (U+{ord(char):04X}) is not a Windows-1252 character")
fixed = bytes(back).decode("utf-8")
cjk = len(re.findall(r"[\u4e00-\u9fff]", fixed))
print("repaired: CJK", cjk, "U+FFFD", fixed.count("\ufffd"), "chars", len(fixed))
if len(sys.argv) > 2 and sys.argv[2] == "--write":
    open(path, "wb").write(fixed.encode("utf-8"))
    print("written (UTF-8, no BOM)")
