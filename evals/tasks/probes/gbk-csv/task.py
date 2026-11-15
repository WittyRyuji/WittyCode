"""A data file that is neither UTF-8 nor LF. What it asks of a harness: read it as what it is, change two records, and
write it back in the same encoding with the same line ends. The file is written here, byte for byte, because the
repository normalises the line ends of its text files."""

from __future__ import annotations

from pathlib import Path

GIVEN = ["品名,库存,单位", "螺丝刀,12,把", "锤子,7,把", "卷尺,30,个", "电钻,4,台", "手套,120,双", "护目镜,45,副", "砂纸,300,张", "胶带,64,卷"]
WANTED = [GIVEN[0], "螺丝刀,35,把", *GIVEN[2:], "扳手,18,把"]
FILE = Path("data") / "stock.csv"


def _bytes(lines: list[str]) -> bytes:
    return "".join(line + "\r\n" for line in lines).encode("gbk")


def build(root: Path) -> None:
    (root / "data").mkdir()
    (root / FILE).write_bytes(_bytes(GIVEN))


def solve(root: Path) -> None:
    (root / FILE).write_bytes(_bytes(WANTED))


def check(root: Path, run) -> str:
    code, output = run(["python", "-m", "unittest", "-q"])
    if code:
        return output
    data = (root / FILE).read_bytes()
    try:
        text = data.decode("gbk")
    except UnicodeDecodeError as error:
        return f"data/stock.csv is no longer GBK text: {error}"
    if data.replace(b"\r\n", b"").count(b"\n") or data.replace(b"\r\n", b"").count(b"\r"):
        return "data/stock.csv no longer has CRLF line ends throughout."
    if text != "".join(line + "\r\n" for line in WANTED):
        return f"data/stock.csv does not hold the wanted records: {text.splitlines()!r}"
    return ""
