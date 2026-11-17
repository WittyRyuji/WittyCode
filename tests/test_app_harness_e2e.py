"""The real page retains an expanded long result across subsequent text-only turn deltas."""
from pathlib import Path

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_large_evidence_stays_visible_after_the_answer(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = await new_conversation(rig)
            (rig.project / "evidence.txt").write_text("HEAD\n" + ("a" * 700 + "\n") * 10 + "MIDDLE EVIDENCE\n" + ("b" * 700 + "\n") * 10 + "TAIL\n", encoding="utf-8")
            model.next(call("read_file", {"file_path": "evidence.txt"}), hold=False)
            model.next(say("Verified the retained evidence."))
            await rig.send("Inspect evidence.txt")
            await rig.wait_text(".block.activity .gtext", "1")
            if not await rig.exists(".block.activity .gbody"):
                await rig.click(".block.activity .gsum")
            await rig.click(".block.activity .irow")
            await rig.wait_text(".idetail", "MIDDLE EVIDENCE")
            model.go()
            await rig.wait_text(".transcript .text-block", "Verified the retained evidence.")
            await rig.wait_for(".send.stop", 0)
            assert "MIDDLE EVIDENCE" in await rig.text(".idetail")
            picture = Path("sandbox/harness-evidence.png")
            picture.parent.mkdir(exist_ok=True)
            assert rig.window.grab().save(str(picture))
        finally:
            await rig.finish()
    run(loop, scenario())
