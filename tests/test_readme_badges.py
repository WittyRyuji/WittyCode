"""README badges must not depend on the wiki server accepting an image request."""

from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SVG = "{http://www.w3.org/2000/svg}"


@pytest.mark.parametrize("readme", ["README.md", "README.zh-CN.md"])
def test_deepwiki_badge_is_bundled_but_links_to_the_wiki(readme):
    text = (ROOT / readme).read_text(encoding="utf-8")
    matches = re.findall(r"\[!\[Ask DeepWiki\]\(([^)]+)\)\]\(([^)]+)\)", text)
    assert matches == [("docs/badges/deepwiki.svg", "https://deepwiki.com/WittyRyuji/WittyCode")]
    assert "https://deepwiki.com/badge.svg" not in text
    root = ET.parse(ROOT / matches[0][0]).getroot()
    assert root.tag == SVG + "svg"
    assert root.attrib["role"] == "img"
    assert root.find(SVG + "title").text == "Ask DeepWiki"
    assert int(root.attrib["width"]) > 0 and int(root.attrib["height"]) > 0
    assert all(node.tag not in {SVG + "script", SVG + "image", SVG + "foreignObject"} for node in root.iter())
    assert all("href" not in key for node in root.iter() for key in node.attrib)
