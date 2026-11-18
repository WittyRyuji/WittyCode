"""The right-hand Agents Team keeps each Sub-Agent's records and prompts usable."""
from __future__ import annotations

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.fakes import until
from tests.test_app_fusion_e2e import select_fusion
from tests.ui_support import run

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("size,with_plan", [((1500, 960), True), ((900, 800), False)])
def test_team_rail_and_agent_prompts_do_not_depend_on_a_plan(qapp, loop, tmp_path, size, with_plan):
    async def scenario():
        rig = await open_rig(tmp_path, size=size)
        try:
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"], "fusionLight": "chatgpt:gpt-6-luna"}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await select_fusion(rig)
            await rig.click(".chip-model")
            await rig.wait_text(".menu-head", "WittyCode")
            assert rig.window.grab().save(str(tmp_path / f"fusion-menu-{size[0]}.png"))
            await rig.press("Escape")
            await rig.click(".chip-permission")
            await rig.click_text(".menu-item", "询问")
            if with_plan:
                rig.plan.script(call("todo_write", {"todos": [{"content": "检查模块", "status": "in_progress"}, {"content": "验证结果", "status": "pending"}]}))
            rig.plan.script(
                call("delegate_task", {"workstream": "Login flow", "paths": ["frontend/auth", "desktop/auth.py"], "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the frontend."}),
                call("shell", {"command": "New-Item team-marker.txt"}),
                say("Frontend report."),
                call("delegate_task", {"workstream": "Regression coverage", "paths": ["tests"], "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the tests."}, call_id="delegate-tests", item_id="fc-tests"),
                call("ask_user", {"questions": [{"header": "Coverage", "question": "Which regression?", "options": ["Unit", "UI"]}]}),
                say("Tests report."),
                say("Team reports checked."),
            )
            await rig.send("Inspect the two modules")
            await rig.wait_text(".conversation-rail .agent-chip", "等你确认")
            assert await rig.text(".agentbar-title") == "Agents Team"
            assert not await rig.exists(".conversation-column .agentbar"), "the team no longer occupies the composer column"
            assert await rig.exists(".planpanel") == with_plan
            if with_plan:
                assert await rig.js("document.querySelector('.agentbar').getBoundingClientRect().top > document.querySelector('.planpanel').getBoundingClientRect().bottom"), "the team is below the plan with a separate divider"
                await rig.click(".plan-close")
                assert await rig.exists(".conversation-rail .agentbar"), "dismissing the plan does not dismiss Agents"
                await rig.click(".plan-toggle:not(.agents-toggle)")
            else:
                await rig.click(".agents-toggle")
                await rig.wait_for(".conversation-rail", 0)
                await rig.click(".agents-toggle")
                await rig.wait_for(".conversation-rail .agentbar")
            assert await rig.js("document.documentElement.scrollWidth <= innerWidth")
            assert rig.window.grab().save(str(tmp_path / f"agents-team-{size[0]}.png"))
            await rig.click(".agent-chip")
            await rig.wait_for(".agentpanel .prompt.perm")
            assert await rig.js("document.querySelector('.agentpanel').getBoundingClientRect().width >= 400"), "the drawer has a usable width, not an implicit grid track"
            assert not (rig.project / "team-marker.txt").exists()
            await rig.click(".agentpanel .prompt.perm .btn.primary")
            await rig.wait_for(".agentpanel .prompt.perm", 0)
            await until(lambda: (rig.project / "team-marker.txt").exists())
            await rig.wait_for(".agentpanel .agent-chip", 2)
            await rig.wait_text(".agentpanel .agent-chip", "等你确认")
            await rig.click(".agentpanel .agent-chip", 1)
            await rig.wait_for(".agentpanel .prompt.ask .otherinput")
            assert await rig.js("document.querySelector('.agentpanel .agent-chip.on .agent-name').textContent === 'Regression coverage'")
            assert await rig.js("document.querySelector('.conversation-column').getBoundingClientRect().width >= 400"), "the narrow inspector overlays rather than crushes the composer"
            await rig.click(".agentpanel .prompt.ask .otherinput")
            await rig.type("UI and keyboard")
            await rig.click_text(".agentpanel .prompt.ask .pbtns .btn", "提交回答")
            await rig.wait_text(".agentpanel-run", "Tests report.")
            await until(lambda: rig.core.order[0].controller.state.value == "ready")
            await rig.click(".agentpanel .agent-chip", 0)
            await rig.wait_text(".agentpanel-run", "Frontend report.")
            assert rig.window.grab().save(str(tmp_path / f"sub-agent-{size[0]}.png"))
            await rig.click('[aria-label="切回主对话"]')
            await rig.wait_for(".agentpanel", 0)
            await rig.wait_for(".conversation-rail .agent-chip", 2)
            await rig.wait_text(".conversation-column .transcript", "Team reports checked.")
            await rig.focus_composer()
            await rig.type("Draft for the main agent")
            await rig.click(".conversation-rail .agent-chip", 1)
            await rig.wait_text(".agentpanel-run", "Tests report.")
            await rig.press("Escape")
            await rig.wait_for(".agentpanel", 0)
            assert await rig.js("document.querySelector('.input').value") == "Draft for the main agent"
            assert not await rig.js("document.querySelector('.agentbar').textContent.includes('子任务')")
            assert await rig.exists(".planpanel") == with_plan
        finally:
            await rig.finish()

    run(loop, scenario())
