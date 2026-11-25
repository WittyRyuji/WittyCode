"""Provider editor model-grid layout in the real Qt page."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests import e2e_support
from tests.test_provider_keys import direct
from tests.test_providers import make_store, model
from tests.ui_support import run

pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows current-user DPAPI")]


@pytest.fixture(autouse=True)
def use_managed_page(monkeypatch):
    if work := os.environ.get("WITTYCODE_TEST_WORK"):
        built = Path(work) / "dist" / "index.html"
        if built.is_file():
            monkeypatch.setattr(e2e_support, "PAGE_FILE", built)


def layout_models():
    return [
        model("demo-l2-long-model-name-for-layout", displayName="L2 长模型名称", contextWindow=64000, maxOutput=8192, reasoning="minimal"),
        model("demo-l3-even-longer-model-name-for-layout", displayName="L3 更长模型名称", contextWindow=128000, maxOutput=16384, reasoning="high"),
    ]


async def open_editor(tmp_path, size):
    store = make_store(tmp_path)
    store.save(direct(models=layout_models()))
    rig = await e2e_support.open_rig(tmp_path, providers=store, size=size)
    await rig.press(",", Qt.KeyboardModifier.ControlModifier)
    await rig.wait_for(".settings")
    await rig.click_text(".snav", "账号与模型")
    await rig.click_text(".prov .btn", "编辑")
    await rig.wait_for(".pe .mtable .mrow", 3)
    return rig


async def add_empty_key_row_if_supported(rig):
    supported = await rig.js('[...document.querySelectorAll(".pe button")].some(e => e.textContent.includes("添加 Key"))')
    if not supported:
        return False
    await rig.click_text(".pe button", "添加 Key")
    await rig.wait_for('.pe .hrow input[type="password"]', 2)
    await rig.wait_for(".pe .mtable.with-keys .mrow", 3)
    return True


async def measure_layout(rig):
    raw = await rig.js(r"""
JSON.stringify((() => {
  const table = document.querySelector(".pe .mtable");
  const page = document.querySelector(".settings-scroll");
  const rows = [...table.querySelectorAll(".mrow")];
  const rect = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.width]; };
  const head = [...rows[0].children].map(rect);
  const mismatch = rows.slice(1).flatMap(row => [...row.children].map((cell, i) => {
    const [hl, hw] = head[i] || [0, 0];
    const [left, width] = rect(cell);
    return Math.max(Math.abs(hl - left), Math.abs(hw - width));
  }));
  const reasoning = [...table.querySelectorAll(".mrow:not(.mhead) .selectbox:not([aria-label])")].map(e => {
    const span = e.querySelector("span");
    return { text: span.textContent, clipped: span.scrollWidth > span.clientWidth + 1, gap: parseFloat(getComputedStyle(e).gap) };
  });
  const keyRows = [...document.querySelectorAll(".pe .hrow")].filter(e => e.querySelector(":scope > input[type=password]")).map(e => {
    const inputs = [...e.querySelectorAll(":scope > input")];
    return { name: inputs[0]?.getBoundingClientRect().width || 0, password: inputs[1]?.getBoundingClientRect().width || 0 };
  });
  return {
    rows: rows.length,
    columns: rows[0].children.length,
    mismatch: Math.max(...mismatch),
    tableClient: table.clientWidth,
    tableScroll: table.scrollWidth,
    pageClient: page.clientWidth,
    pageScroll: page.scrollWidth,
    withKeys: table.classList.contains("with-keys"),
    reasoning,
    keyRows,
  };
})())
""")
    return json.loads(raw)


def assert_common_layout(stats, keyed):
    assert stats["rows"] == 3
    assert stats["mismatch"] <= 1.5, "header and model-row cells keep the same x positions and widths"
    assert len(stats["reasoning"]) == 2
    assert all(item["text"] and not item["clipped"] for item in stats["reasoning"]), "reasoning labels are not truncated"
    assert all(item["gap"] <= 6.5 for item in stats["reasoning"])
    assert stats["pageScroll"] <= stats["pageClient"] + 1, "the settings page itself does not scroll sideways"
    assert stats["columns"] == (8 if keyed else 7)
    if keyed:
        assert stats["withKeys"]
        assert len(stats["keyRows"]) == 2
        assert all(row["password"] > row["name"] for row in stats["keyRows"]), "the password field gets the remaining width"


async def check_layout(tmp_path, size, expect_table_scroll, evidence_name):
    rig = await open_editor(tmp_path, size)
    try:
        keyed = await add_empty_key_row_if_supported(rig)
        stats = await measure_layout(rig)
        assert_common_layout(stats, keyed)
        overflow = stats["tableScroll"] - stats["tableClient"]
        if expect_table_scroll:
            assert overflow > 1, "narrow table scrolls inside itself"
        else:
            assert overflow <= 1, "wide table fits without a horizontal scrollbar"
        if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
            await rig.js('document.querySelector(".pe .mtable").scrollIntoView({block: "center", inline: "nearest"})')
            await rig.shot(".settings", Path(evidence) / evidence_name)
    finally:
        await rig.finish()


def test_provider_editor_model_grid_fits_wide(qapp, loop, tmp_path):
    run(loop, check_layout(tmp_path, (1500, 980), False, "provider-layout-wide.png"))


def test_provider_editor_model_grid_scrolls_inside_narrow(qapp, loop, tmp_path):
    run(loop, check_layout(tmp_path, (860, 980), True, "provider-layout-narrow.png"))
