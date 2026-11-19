"""Shipping nest assets are repeatable, animated and distinguishable without colour."""

from itertools import combinations
from pathlib import Path
from runpy import run_path

from PySide6.QtGui import QImage, QImageReader


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "frontend/src/assets"
ROLES = ("coder", "quality-sampler", "runner", "observer")


def rgba(image):
    assert (image.width(), image.height()) == (48, 48)
    # Qt does not guarantee RGB below a transparent GIF pixel after disposal.
    return tuple(pixel if pixel >> 24 else 0
                 for y in range(48) for x in range(48) for pixel in (image.pixel(x, y),))


def test_role_nests_regenerate_without_touching_generic(tmp_path):
    script = run_path(str(ROOT / "frontend/scripts/generate_dragon_nest.py"))
    assets = tmp_path / "frontend/src/assets"
    script["generate"](tmp_path, sheet=False)
    assert not list(assets.glob("dragon-nest.*"))
    script["generate"](tmp_path, generic=True)
    for name in ("dragon-nest", *(f"dragon-nests/{role}" for role in ROLES)):
        for ext in ("gif", "png"):
            path = f"{name}.{ext}"
            assert (assets / path).read_bytes() == (ASSETS / path).read_bytes(), path


def test_role_nests_have_distinct_alpha_shapes_and_matching_animated_first_frames():
    masks = {"generic": tuple(map(bool, rgba(QImage(str(ASSETS / "dragon-nest.png")))))}
    for role in ROLES:
        path = ASSETS / "dragon-nests" / role
        still = rgba(QImage(str(path.with_suffix(".png"))))
        assert set(pixel >> 24 for pixel in still) == {0, 255}, role
        masks[role] = tuple(map(bool, still))
        gif = QImageReader(str(path.with_suffix(".gif")))
        assert gif.supportsAnimation() and gif.imageCount() == 12 and gif.loopCount() == -1, role
        frames, durations = [], []
        for _ in range(gif.imageCount()):
            frames.append(rgba(gif.read()))
            durations.append(gif.nextImageDelay())
        assert frames[0] == still, role
        assert sum(durations) == 2200 and len(set(frames)) == 7, role  # Rest, plus three eye poses per dragon.
        assert all(frame[36 * 48:] == still[36 * 48:] for frame in frames), role  # The nest stays put.
        assert all({pixel >> 24 for pixel in frame} == {0, 255} for frame in frames), role
    for (left, a), (right, b) in combinations(masks.items(), 2):
        # Require real silhouette changes, not just a recolour or a couple of dots.
        assert sum(x != y for x, y in zip(a, b)) >= 32 * 4, (left, right)
