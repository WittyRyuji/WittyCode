"""Opt-in whole-window check: build isolated About pages without reading local dotenv files."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from tests import e2e_support
from tests.ui_support import outside_task, run, wait_for

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
GITHUB = "https://github.com/WittyRyuji/WittyCode"
CONTACT_ENV = "VITE_WITTYCODE_CONTACT_EMAIL"
pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(os.getenv("WITTYCODE_ABOUT_E2E") != "1", reason="set WITTYCODE_ABOUT_E2E=1 for the isolated Qt build check"),
]


def build_page(tmp_path, email):
    node = shutil.which("node")
    assert node and (FRONTEND / "node_modules" / "vite").is_dir(), "install the pinned Node and frontend dependencies first"
    env_dir, out_dir = tmp_path / "env", tmp_path / "page"
    env_dir.mkdir()
    env = {key: value for key, value in os.environ.items() if key.upper() != CONTACT_ENV}
    if email is not None:
        env[CONTACT_ENV] = email
    subprocess.run(
        [node, "--input-type=module", "-e", """
          import { build } from 'vite';
          await build({configLoader: 'runner', envDir: process.argv[1], build: {outDir: process.argv[2], emptyOutDir: false}});
        """, str(env_dir), str(out_dir)],
        cwd=FRONTEND, env=env, check=True, timeout=90,
    )
    return out_dir / "index.html"


async def select_link(rig, index):
    coords = json.loads(await rig.js(f"""(() => {{
      const text = document.querySelectorAll('.about-link-text')[{index}];
      text.scrollIntoView({{block: 'nearest'}});
      const range = document.createRange();
      range.selectNodeContents(text);
      const lines = range.getClientRects(), first = lines[0], last = lines[lines.length - 1];
      return JSON.stringify([first.left + 1, first.top + first.height / 2, last.right - 1, last.top + last.height / 2]);
    }})()"""))
    x1, y1, x2, y2 = (round(value * rig.window.view.zoomFactor()) for value in coords)
    start, end = QPoint(x1, y1), QPoint(x2, y2)
    target = rig.window.view.focusProxy() or rig.window.view
    left, no_modifiers = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    await outside_task(lambda: QTest.mouseMove(target, start))
    await outside_task(lambda: QTest.mousePress(target, left, no_modifiers, start))
    await asyncio.sleep(0.05)
    await outside_task(lambda: QTest.mouseMove(target, end))
    await asyncio.sleep(0.05)
    await outside_task(lambda: QTest.mouseRelease(target, left, no_modifiers, end))


@pytest.mark.parametrize("email", [None, "demo@example.com"], ids=["fallback", "configured"])
def test_about_page(qapp, loop, tmp_path, monkeypatch, email):
    monkeypatch.setattr(e2e_support, "PAGE_FILE", build_page(tmp_path, email))

    async def scenario():
        rig = await e2e_support.open_rig(tmp_path, size=(900, 680))
        try:
            opened = []
            rig.window.platform.open_url = opened.append
            ctrl = Qt.KeyboardModifier.ControlModifier
            await rig.press(",", ctrl)
            await rig.click_text(".snav", "关于")
            assert await rig.text(".settings-body h2") == "关于"
            assert await rig.texts(".about .kvs > div > span") == ["版本", "联系方式", "项目 GitHub", "日志"]
            version = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))["version"]
            assert await rig.text(".about .kvs b") == version
            links = [(email, f"mailto:{email}") if email else ("通过 GitHub Issues 联系", f"{GITHUB}/issues"), (GITHUB, GITHUB)]
            assert await rig.texts(".about-link-text") == [text for text, _ in links]
            assert await rig.js("(() => { const s = document.querySelector('.settings-scroll'); return s.scrollWidth <= s.clientWidth; })()")
            for index, (text, href) in enumerate(links):
                await select_link(rig, index)
                await wait_for(lambda: rig.js(f"getSelection().toString() === {json.dumps(text)}"), message="mouse-select the entire contact/link")
                assert len(opened) == index, "dragging to select must not launch an external application"
                # Confirm Ctrl+C reaches the selection without changing the user's system clipboard.
                await rig.js("document.addEventListener('copy', event => { window.aboutCopied = getSelection().toString(); event.preventDefault(); }, {once: true})")
                await rig.press("c", ctrl)
                assert await rig.js("window.aboutCopied") == text
                await rig.js("getSelection().removeAllRanges()")
                await rig.click(".about .kvs a", index)
                await wait_for(lambda: len(opened) == index + 1, message="forward the link to the desktop shell")
                assert opened[-1] == href
            assert await rig.text(".settings-body h2") == "关于", "the web view must not navigate"
            evidence = os.getenv("WITTYCODE_TEST_EVIDENCE")
            if evidence:
                path = Path(evidence) / ("about-configured.png" if email else "about-fallback.png")
                assert (await outside_task(rig.window.grab)).save(str(path))
        finally:
            await rig.finish()

    run(loop, scenario())
