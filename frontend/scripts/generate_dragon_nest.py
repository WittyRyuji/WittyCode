"""Generate the four role nests and the labeled sheet (use the project's Python/Qt).

    python frontend/scripts/generate_dragon_nest.py

Pass --generic only to explicitly regenerate the original generic assets instead.
"""

import argparse
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
GENERIC_PALETTE = bytes.fromhex("".join(COLOURS.values()))
COLOURS.update({
    "n": "22283F", "m": "4F678F", "i": "48CCDB", "j": "C0FFEE",
    "f": "FA8934", "e": "C34B39", "v": "F55370", "u": "9D2C56",
    "g": "EFD75B", "t": "A87339", "l": "8FCBFE", "p": "E899DD",
})
INDEX = {key: i for i, key in enumerate(COLOURS)}
PALETTE = bytes.fromhex("".join(COLOURS.values()))

# Slug, identity in the UI, and caption for the side-by-side sheet.
ROLES = (
    ("coder", "Coder", "Goggles + keyboard"),
    ("quality-sampler", "QualitySampler (QS)", "Magnifying glass"),
    ("runner", "Runner", "Swept wing + scarf"),
    ("observer", "Observer", "Telescope + lookout"),
)

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

# Props are on the same 24-dot grid, with large silhouettes so they read even at 1×.
GOGGLES = (
    ".nn.nnnn",
    "njmnijmn",
    ".nn.nnn.",
)
KEYBOARD = (
    ".nnnnnnnnnnnnnn.",
    "nljmjmljmjmljmjn",
    "nmmmmjjjjmmmmmmn",
    ".nnnnnnnnnnnnnn.",
)
WORKBENCH = (
    "rccccccccccccccccccccccr",
    "rsr..rsscchsscchssr..rsr",
    ".rr...rrscsscssrr....rr.",
    ".......rrrrrrrrrr.......",
)
MAGNIFIER = (
    "..nnnn....",
    ".njiiin...",
    "nj..iiin..",
    "ni....in..",
    "ni....in..",
    "ni...jin..",
    ".niiinnn..",
    "..nnn.nmn.",
    ".......nmn",
    "........nn",
)
WING = (
    "......kn",
    "....kkii",
    "..kkiijn",
    ".kiijjn.",
    "kiijjn..",
    "kijnkn..",
    "kin..n..",
    "kn......",
)
SCARF = (
    ".........kk",
    "..nnn...kvk",
    ".kvuvnnnvvk",
    "kvvuvvvvuuk",
    "nnnnnuuunn.",
    ".....nnn...",
)
TELESCOPE = (
    ".nnn........",
    "njiinnnnn...",
    "nimiillmmnnn",
    "nimiimmmmnnn",
    "nimin.nnn...",
    ".nnn........",
)
LOOKOUT = (
    "....rccccccccccccccr....",
    ".....rsr........rsr.....",
    ".....rssrrrrrrrrssr.....",
    ".....rsrrhhhhhhrrsr.....",
    ".....rr..........rr.....",
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


def draw_frame(front, rear, role=None):
    pixels = [bytearray(SIDE) for _ in range(SIDE)]
    front_lift, rear_lift = front != "rest", rear != "rest"

    def layer(x, y, rows):
        for dy, row in enumerate(rows):
            for dx, key in enumerate(row):
                if key != ".":
                    pixels[y + dy][x + dx] = INDEX[key]

    def dragon(x, y, rows, pose, colours="bda"):
        rows = list(rows)
        if pose in ("half", "shut"):
            lower = "kwb" if pose == "half" else "kkk"
            rows[4] = rows[4][:4] + "bbb" + rows[4][7:]
            rows[5] = rows[5][:4] + lower + rows[5][7:]
        recolour = str.maketrans("bda", colours)
        layer(x, y - (pose != "rest"), [row.translate(recolour) for row in rows])

    rise = 3 if role == "observer" else 0
    layer(0, 12 - rise, BACK_RIM)
    if role == "coder":
        dragon(11, 3, REAR_DRAGON, rear, "bda")
        dragon(2, 6, FRONT_DRAGON, front, "feg")
        layer(4, 8 - front_lift, GOGGLES)
    elif role == "quality-sampler":
        dragon(11, 4, REAR_DRAGON, rear, "gth")
        dragon(5, 6, FRONT_DRAGON, front, "BDp")
        layer(0, 6 - front_lift, MAGNIFIER)
    elif role == "runner":
        layer(16, 1 - rear_lift, WING)
        dragon(10, 4, REAR_DRAGON, rear, "imj")
        dragon(4, 7, FRONT_DRAGON, front, "fea")
        layer(0, 13 - front_lift, SCARF)
    elif role == "observer":
        dragon(11, 2, REAR_DRAGON, rear, "mlj")
        dragon(3, 5, FRONT_DRAGON, front, "BDp")
        layer(3, 3 - rear_lift, TELESCOPE)
    else:
        dragon(11, 4, REAR_DRAGON, rear, "BDA")
        dragon(2, 7, FRONT_DRAGON, front)
    layer(0, 15 - rise, FRONT_WEAVE)
    if role == "coder":
        layer(0, 18, WORKBENCH)
        layer(4, 15, KEYBOARD)
    elif role == "observer":
        layer(0, 18, LOOKOUT)
    return pixels


def scale_frame(rows, scale):
    return [b"".join(bytes([i]) * scale for i in row) for row in rows for _ in range(scale)]


def write_png(path, rows, palette=PALETTE):
    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", crc32(kind + data))

    size = len(rows)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join((
        chunk(b"IHDR", pack(">IIBBBBB", size, size, 8, 3, 0, 0, 0)),
        chunk(b"PLTE", palette), chunk(b"tRNS", b"\x00"),
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


def write_gif(path, frames, scale, palette=PALETTE):
    size = SIDE * scale
    dimensions = pack("<HH", size, size)
    data = bytearray(b"GIF89a" + dimensions + b"\xf7\x00\x00" + palette.ljust(768, b"\0"))
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


def write_role_sheet(path, assets):
    """The labels use the system font; the sprites are drawn at integer scales."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    sheet = QImage(860, 360, QImage.Format.Format_RGB32)
    sheet.fill(QColor("#e9edf2"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    def label(box, text, size, colour, bold=False, centered=True):
        font = QFont()
        font.setPixelSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        alignment = Qt.AlignmentFlag.AlignCenter if centered else Qt.AlignmentFlag.AlignVCenter
        painter.drawText(box, alignment, text)

    label(QRect(24, 12, 812, 34), "Agents Team  /  Role nests", 23, "#1d2739", True, False)
    label(QRect(24, 46, 812, 23), "Opening frames    |    3× detail above    ·    1× right-rail size below", 14, "#47566c", False, False)
    for index, (slug, identity, caption) in enumerate(ROLES):
        x = 24 + index * 206
        painter.fillRect(x, 83, 194, 253, QColor("#fbfcfe"))
        sprite = QImage(str(assets / f"{slug}.png"))
        painter.drawImage(QRect(x + 25, 88, 144, 144), sprite)
        label(QRect(x, 235, 194, 23), identity, 15, "#1d2739", True)
        label(QRect(x, 258, 194, 20), caption, 13, "#47566c")
        painter.fillRect(x + 72, 282, 50, 50, QColor("#536174"))
        painter.fillRect(x + 73, 283, 48, 48, QColor("#e9edf2"))
        painter.drawImage(x + 73, 283, sprite)
    painter.end()
    if not sheet.save(str(path)):
        raise OSError(f"Could not write {path}")


def generate(root, *, generic=False, sheet=True):
    assets = root / "frontend/src/assets"
    preview = root / "outputs/agent-nest-preview"
    if generic:
        assets.mkdir(parents=True, exist_ok=True)
        preview.mkdir(parents=True, exist_ok=True)
        frames = [(draw_frame(front, rear), duration) for front, rear, duration in TIMELINE]
        write_gif(assets / "dragon-nest.gif", frames, 2, GENERIC_PALETTE)
        write_png(assets / "dragon-nest.png", scale_frame(frames[0][0], 2), GENERIC_PALETTE)
        write_gif(preview / "dragon-nest.gif", frames, 8, GENERIC_PALETTE)
        return
    assets /= "dragon-nests"
    assets.mkdir(parents=True, exist_ok=True)
    for role, _, _ in ROLES:
        frames = [(draw_frame(front, rear, role), duration) for front, rear, duration in TIMELINE]
        write_gif(assets / f"{role}.gif", frames, 2)
        write_png(assets / f"{role}.png", scale_frame(frames[0][0], 2))
    if sheet:
        preview.mkdir(parents=True, exist_ok=True)
        write_role_sheet(preview / "role-nests.png", assets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generic", action="store_true", help="Regenerate only the original generic assets.")
    args = parser.parse_args()
    if not args.generic:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication(["generate_dragon_nest"])  # No window; use the system's font backend.
    generate(Path(__file__).resolve().parents[2], generic=args.generic)
