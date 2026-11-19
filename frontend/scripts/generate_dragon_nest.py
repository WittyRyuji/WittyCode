"""Regenerate the Agents Team nest assets: python frontend/scripts/generate_dragon_nest.py."""

from pathlib import Path
from struct import pack
from zlib import compress, crc32


SIDE = 24
COLOURS = {
    ".": "000000",  # Transparent palette index 0 in both formats.
    "k": "101018", "w": "FCFCFC",
    # Base colours and horn highlights match the green and purple dragon sprites.
    "b": "00A844", "d": "007C46", "a": "F8D878",
    "B": "6844FC", "D": "4930B9", "A": "F8B8F8",
    "r": "583524", "o": "693E2C", "s": "A05A37", "c": "CF884A", "h": "F3BF72",
}
INDEX = {key: i for i, key in enumerate(COLOURS)}
PALETTE = bytes.fromhex("".join(COLOURS.values()))

FRONT_DRAGON = (
    ".........kk.",
    "...kkkk.kaak",
    "..kbbbbkaak.",
    ".kbbbbbbak..",
    ".kbbkwwbbbk.",
    "kbbbkwwbbbk.",
    "kbkbbbbbbbk.",
    ".kkkkbbdbbk.",
    "..kbbddbbbk.",
    "...kkaaddbk.",
    "....kaaddbbk",
    "....kaaddbbk",
)
REAR_DRAGON = (
    ".........kk.",
    "....kk..kak.",
    "...kak.kabbk",
    "..kbbbkbbbk.",
    ".kbbkwwbbbk.",
    "kbbbkwwbbbk.",
    "kbkbbbbbbbk.",
    ".kkkkbbdbbk.",
    "..kbbddbbbk.",
    "...kkaaddbbk",
    "....kaaddbbk",
    "....kaaddbbk",
    ".....kaddbbk",
)
BACK_RIM = (
    "........rrssrr..........",
    ".....rrscoooooosrr......",
    "...rscooooooooooocsrr...",
    "..rsoooooooooooooooosr..",
    ".rsoooooooooooooooooosr.",
)
FRONT_WEAVE = (
    ".rhh................hhr.",
    ".rschhcccccccccccchhcsr.",
    "..rhchhchhchhchhchhchr..",
    "...rsccssccssccssccsr...",
    "....rsscchsscchsscsr....",
    ".....rrssccssccssrr.....",
    ".......rrrrrrrrrr.......",
)

# (Front pose, rear pose, hold in ms). The rim never moves; each peek is one dot high.
TIMELINE = (
    ("rest", "rest", 540),
    ("open", "rest", 240), ("half", "rest", 60), ("shut", "rest", 100),
    ("half", "rest", 60), ("open", "rest", 200),
    ("rest", "rest", 300),
    ("rest", "open", 240), ("rest", "half", 60), ("rest", "shut", 110),
    ("rest", "half", 60), ("rest", "open", 230),
)


def draw_frame(front, rear):
    pixels = [bytearray(SIDE) for _ in range(SIDE)]

    def layer(x, y, rows):
        for dy, row in enumerate(rows):
            for dx, key in enumerate(row):
                if key != ".":
                    pixels[y + dy][x + dx] = INDEX[key]

    def dragon(x, y, rows, pose, purple=False):
        rows = list(rows)
        if pose in ("half", "shut"):
            lower = "kwb" if pose == "half" else "kkk"
            rows[4] = rows[4][:4] + "bbb" + rows[4][7:]
            rows[5] = rows[5][:4] + lower + rows[5][7:]
        if purple:
            recolour = str.maketrans("bda", "BDA")
            rows = [row.translate(recolour) for row in rows]
        layer(x, y - (pose != "rest"), rows)

    layer(0, 12, BACK_RIM)
    dragon(11, 4, REAR_DRAGON, rear, purple=True)
    dragon(2, 7, FRONT_DRAGON, front)
    layer(0, 15, FRONT_WEAVE)
    return pixels


def scale_frame(rows, scale):
    return [b"".join(bytes([i]) * scale for i in row) for row in rows for _ in range(scale)]


def write_png(path, rows):
    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", crc32(kind + data))

    size = len(rows)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join((
        chunk(b"IHDR", pack(">IIBBBBB", size, size, 8, 3, 0, 0, 0)),
        chunk(b"PLTE", PALETTE), chunk(b"tRNS", b"\x00"),
        chunk(b"IDAT", compress(b"".join(b"\x00" + row for row in rows))),
        chunk(b"IEND", b""),
    )))


def gif_lzw(pixels):
    # As in preview-dragon-gifs.mjs, clear before the dictionary needs 10-bit codes.
    clear, end = 256, 257
    table, next_code = {}, 258
    output, hold, bits = bytearray(), 0, 0

    def write(code):
        nonlocal hold, bits
        hold |= code << bits
        bits += 9
        while bits >= 8:
            output.append(hold & 255)
            hold >>= 8
            bits -= 8

    pixels = iter(pixels)
    write(clear)
    prefix = next(pixels)
    for pixel in pixels:
        pair = (prefix, pixel)
        if pair in table:
            prefix = table[pair]
            continue
        write(prefix)
        if next_code == 511:
            write(clear)
            table.clear()
            next_code = 258
        else:
            table[pair] = next_code
            next_code += 1
        prefix = pixel
    write(prefix)
    write(end)
    if bits:
        output.append(hold)
    return output


def write_gif(path, frames, scale):
    size = SIDE * scale
    dimensions = pack("<HH", size, size)
    data = bytearray(b"GIF89a" + dimensions + b"\xf7\x00\x00" + PALETTE.ljust(768, b"\0"))
    data += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"  # Infinite loop.
    for rows, duration in frames:
        # Full frames with transparent index 0 and disposal 2, so raised horns leave no trail.
        data += b"\x21\xf9\x04\x09" + pack("<H", duration // 10) + b"\0\0"
        data += b"\x2c\0\0\0\0" + dimensions + b"\0\x08"
        compressed = gif_lzw(b"".join(scale_frame(rows, scale)))
        for start in range(0, len(compressed), 255):
            block = compressed[start:start + 255]
            data.append(len(block))
            data += block
        data.append(0)
    path.write_bytes(data + b"\x3b")


def generate(root):
    assets = root / "frontend/src/assets"
    preview = root / "outputs/agent-nest-preview"
    assets.mkdir(parents=True, exist_ok=True)
    preview.mkdir(parents=True, exist_ok=True)
    frames = [(draw_frame(front, rear), duration) for front, rear, duration in TIMELINE]
    write_gif(assets / "dragon-nest.gif", frames, 2)
    write_png(assets / "dragon-nest.png", scale_frame(frames[0][0], 2))
    write_gif(preview / "dragon-nest.gif", frames, 8)


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[2])
