"""Edit a built-in identity through the real settings page and reopen its saved version."""
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_builtin_expert_edits_text_without_replacing_identity(qapp, loop, tmp_path):
    async def show_catalog(rig):
        await rig.press(",", Qt.KeyboardModifier.ControlModifier)
        await rig.wait_for(".settings")
        await rig.click_text(".snav", "Fusion")
        await rig.wait_for('[aria-label="专家角色提示词"]:enabled')

    async def scenario():
        rig = await open_rig(tmp_path, size=(1360, 1020))
        try:
            await show_catalog(rig)
            stable = await rig.js('document.querySelector("[aria-label=专家稳定标识]").value')
            assert stable == 'Coder'
            assert await rig.exists('[aria-label="专家稳定标识"]:disabled')
            assert await rig.count('[aria-label="专家工作类型"], [aria-label="专家启用"]') == 0
            for label, text in (('专家显示名', 'Inventory Writer'), ('专家描述', 'Maintains demo stock.'),
                                ('专家角色提示词', 'Check the inventory before edits.')):
                await rig.click(f'[aria-label="{label}"]')
                await rig.press('A', Qt.KeyboardModifier.ControlModifier)
                await rig.type(text)
            await rig.click_text('.expert-editor button', '保存新版本')
            await rig.wait_text('.expert-catalog [role=status]', '已保存版本 1')
            await rig.press('Escape')
            await rig.wait_for('.settings', 0)
            await show_catalog(rig)
            await rig.wait_text('.expert-editor h4', 'Inventory Writer')
            assert await rig.js('document.querySelector("[aria-label=专家描述]").value') == 'Maintains demo stock.'
            assert await rig.js('document.querySelector("[aria-label=专家角色提示词]").value') == 'Check the inventory before edits.'
            assert await rig.js('document.querySelector("[aria-label=专家稳定标识]").value') == 'Coder'
            if evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE'):
                await rig.js('document.querySelector(".expert-editor").scrollIntoView({block:"center"})')
                await rig.shot('.expert-editor', Path(evidence) / 'builtin-expert-editor.png')
        finally:
            await rig.finish()

    run(loop, scenario())
