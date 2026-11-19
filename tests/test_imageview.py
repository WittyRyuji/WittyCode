"""Real Qt and bridge coverage for side-panel picture formats."""
import base64
import gzip
import struct

import pytest
from PySide6.QtGui import QImage

from desktop.core import files
from desktop.core.app_core import AppError
from desktop.ui.imageview import picture_png
from tests.test_app_files import conversation, run


@pytest.mark.parametrize("suffix", ["APNG", "AVIF", "JFIF", "JPE", "CUR", "TIF", "TIFF", "TGA", "PBM", "PGM", "PPM", "XBM", "XPM", "ICNS", "WBMP", "SVGZ"])
def test_format_classification(tmp_path, suffix):
    path = tmp_path / f"sample.{suffix}"
    path.touch()
    assert files.preview_kind(path) == "image"


def test_tiff_is_actually_converted_to_png(tmp_path):
    path = tmp_path / "sample.tiff"
    source = QImage(11, 7, QImage.Format.Format_RGB32)
    source.fill(0xFF2080DE)
    assert source.save(str(path), "TIFF")
    header, payload = picture_png(str(path)).split(",")
    result = QImage.fromData(base64.b64decode(payload))
    assert header == "data:image/png;base64"
    assert result.size() == source.size() and result.pixel(0, 0) == 0xFF2080DE


def test_tga_without_a_v2_footer_and_compressed_svg(tmp_path):
    tga = tmp_path / "sample.TGA"
    # One top-left-origin BGR pixel, deliberately without the v2 footer.
    tga.write_bytes(struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 1, 1, 24, 32) + b"\xee\x99\x33")
    svgz = tmp_path / "sample.svgz"
    svgz.write_bytes(gzip.compress(b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>'))
    for source, expected in ((tga, (1, 1)), (svgz, (8, 8))):
        url = picture_png(str(source))
        assert QImage.fromData(base64.b64decode(url.split(",", 1)[1])).size().toTuple() == expected


def test_icns_plugin_can_decode_a_picture_without_reporting_its_size_first(tmp_path):
    source = tmp_path / "sample.icns"
    image = QImage(16, 16, QImage.Format.Format_RGB32)
    image.fill(0xFF264A71)
    assert image.save(str(source), "ICNS")
    url = picture_png(str(source))
    assert QImage.fromData(base64.b64decode(url.split(",", 1)[1])).size().toTuple() == (16, 16)


def test_pixel_limit_and_damaged_image(tmp_path):
    path = tmp_path / "huge.ppm"
    path.write_bytes(b"P6\n10000 10000\n255\n\x00")
    with pytest.raises(files.FileProblem, match="像素太多"):
        picture_png(str(path))
    path = tmp_path / "broken.tiff"
    path.write_bytes(b"not an image")
    with pytest.raises(files.FileProblem, match="不能在侧栏里预览"):
        picture_png(str(path))


def test_new_picture_routes_and_heic_message(tmp_path):
    async def scenario():
        core, cid, platform = await conversation(tmp_path)
        project = tmp_path / "proj"
        try:
            for name, mime in [("sample.APNG", "image/apng"), ("sample.AVIF", "image/avif"), ("sample.JFIF", "image/jpeg"), ("sample.CUR", "image/x-icon"), ("sample.TIFF", "image/png")]:
                payload = b"sample"
                (project / name).write_bytes(payload)
                args = {"id": cid, "path": name}
                shown = await core.call("files.read", args)
                assert (shown["kind"], shown["size"]) == ("image", len(payload))
                page = await core.call("files.page", args)
                expected = "CONVERTED" if name.endswith(".TIFF") else base64.b64encode(payload).decode()
                assert page["url"] == f"data:{mime};base64,{expected}"
            assert "picture:sample.TIFF" in platform.calls
            (project / "sample.HEIC").write_bytes(b"picture")
            with pytest.raises(AppError, match="HEIC 图片不能在侧栏里预览"):
                await core.call("files.read", {"id": cid, "path": "sample.HEIC"})
        finally:
            await core.shutdown()
    run(scenario())
