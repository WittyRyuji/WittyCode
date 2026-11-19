"""The right-hand Agents Team keeps each Sub-Agent's records and prompts usable."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import call, say, usage
from tests.fakes import until
from tests.test_app_fusion_e2e import select_fusion
from tests.ui_support import run

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("size,with_plan", [((1440, 900), True), ((1100, 600), False)])
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
            brief = "Inspect the frontend.\n\n" + "\n\n".join(
                f"Sample {i + 1}: " + "Compare the example data to the expected results and record the differences. " * 10
                for i in range(10)
            )
            rig.plan.script(
                call("delegate_task", {"workstream": "Login flow", "paths": ["frontend/auth", "desktop/auth.py"], "model": "chatgpt:gpt-6-luna", "role": "worker", "task": brief}),
                call("shell", {"command": "New-Item team-marker.txt"}, used=usage(260, 35, 50)),
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
            assert not await rig.exists(".agent-flow .react-flow__node-nest")
            assert await rig.text(".agent-flow button .agent-flow-model") == "工作专家"  # Legacy delegate_task carries only a role.
            assert rig.window.grab().save(str(tmp_path / f"agents-team-{size[0]}.png"))
            await rig.click(".agent-chip")
            await rig.wait_for(".agentpanel .prompt.perm")
            await rig.wait_text(".agentpanel-tokens", "合计 295 Token")  # The expert is still waiting at the permission prompt.
            await rig.wait_text(".agentpanel .agent-chip.on .agent-chip-tokens", "295 Token")
            await rig.wait_text(".agentpanel .agentbar-tokens", "295 Token")
            assert await rig.js("""(() => {
                const panel = document.querySelector('.agentpanel');
                const team = panel.querySelector('.agentbar');
                const head = team.querySelector('.agentbar-head'), chip = team.querySelector('.agent-chip');
                const task = panel.querySelector('.agentpanel-task-text'), run = panel.querySelector('.agentpanel-run');
                return panel.scrollHeight <= panel.clientHeight + 1 && panel.clientWidth >= 400 &&
                    team.clientHeight >= head.offsetHeight + chip.offsetHeight && run.clientHeight >= 160 &&
                    task.textContent.length >= 6000 && task.scrollHeight > task.clientHeight;
            })()"""), "a long brief leaves both inspector panes usable"
            await rig.click(".agentpanel-task-toggle")
            assert await rig.js("""(() => {
                const panel = document.querySelector('.agentpanel');
                const task = panel.querySelector('.agentpanel-task-text.expanded');
                return task && task.scrollHeight > task.clientHeight && panel.scrollHeight <= panel.clientHeight + 1 &&
                    panel.querySelector('.agentpanel-run').clientHeight > 0;
            })()""")
            await rig.click(".agentpanel-task-toggle")
            assert not (rig.project / "team-marker.txt").exists()
            await rig.click(".agentpanel .prompt.perm .btn.primary")
            await rig.wait_for(".agentpanel .prompt.perm", 0)
            await until(lambda: (rig.project / "team-marker.txt").exists())
            await rig.wait_text(".agentpanel .agent-chip", "等你确认")
            assert await rig.js("document.querySelector('.agentpanel .agent-history-toggle').getAttribute('aria-expanded') === 'false'")
            assert await rig.texts(".agentpanel .agent-name") == ["Regression coverage"]
            await rig.click(".agentpanel .agent-history-toggle")
            await rig.wait_for(".agentpanel .agent-chip", 2)
            assert await rig.texts(".agentpanel .agent-name") == ["Regression coverage", "Login flow"]
            assert await rig.js("document.querySelector('.agentpanel .agent-list').getBoundingClientRect().bottom <= document.querySelector('.agentpanel .agent-history').getBoundingClientRect().top")
            await rig.click(".agentpanel .agent-chip", 0)
            await rig.wait_for(".agentpanel .prompt.ask .otherinput")
            assert await rig.js("document.querySelector('.agentpanel .agent-chip.on .agent-name').textContent === 'Regression coverage'")
            assert await rig.js("document.querySelector('.conversation-column').getBoundingClientRect().width >= 400"), "the narrow inspector overlays rather than crushes the composer"
            await rig.click(".agentpanel .prompt.ask .otherinput")
            await rig.type("UI and keyboard")
            await rig.click_text(".agentpanel .prompt.ask .pbtns .btn", "提交回答")
            await rig.wait_text(".agentpanel-run", "Tests report.")
            await until(lambda: rig.core.order[0].controller.state.value == "ready")
            await rig.click_text(".agentpanel .agent-chip", "Login flow")
            await rig.wait_text(".agentpanel-run", "Frontend report.")
            assert rig.window.grab().save(str(tmp_path / f"sub-agent-{size[0]}.png"))
            await rig.click('[aria-label="切回主对话"]')
            await rig.wait_for(".agentpanel", 0)
            await rig.wait_for(".conversation-rail .agent-chip", 0)
            assert await rig.js("document.querySelector('.conversation-rail .agent-history-toggle').getAttribute('aria-expanded') === 'false'")
            await rig.click(".conversation-rail .agent-history-toggle")
            await rig.wait_for(".conversation-rail .agent-chip", 2)
            await rig.press(Qt.Key.Key_Space)
            await rig.wait_for(".conversation-rail .agent-chip", 0)
            await rig.press("Enter")
            await rig.wait_for(".conversation-rail .agent-chip", 2)
            await rig.wait_text(".conversation-column .transcript", "Team reports checked.")
            await rig.focus_composer()
            await rig.type("Draft for the main agent")
            await rig.click_text(".conversation-rail .agent-chip", "Regression coverage")
            await rig.wait_text(".agentpanel-run", "Tests report.")
            await rig.press("Escape")
            await rig.wait_for(".agentpanel", 0)
            assert await rig.js("document.querySelector('.input').value") == "Draft for the main agent"
            assert not await rig.js("document.querySelector('.agentbar').textContent.includes('子任务')")
            assert await rig.exists(".planpanel") == with_plan
        finally:
            await rig.finish()

    run(loop, scenario())


@pytest.mark.fusion_gates("brief")
def test_team_receives_fusion_record_identities(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            model = "chatgpt:gpt-6-luna"
            for key, value in {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": [model], "fusionLight": model}.items():
                await rig.core.call("settings.set", {"key": key, "value": value})
            await select_fusion(rig)

            def delegate(identity, workstream):
                return call("delegate_task", {
                    "identity": identity, "workstream": workstream, "goal": "Check a local example.",
                    "constraints": "Do not write files.", "acceptance": "Report the result.",
                    "paths": [], "read_only": True, "budget": {"steps": 8},
                }, call_id=f"delegate-{workstream}", item_id=f"fc-{workstream}")

            rig.plan.script(delegate("Coder", "Inventory"), say("Inventory expert result."),
                            delegate("Runner", "Coverage"), say("Coverage expert result."),
                            delegate("Coder", "Login"), say("Login expert result."), say("Identity reports checked."))
            await rig.send("Check the three examples")
            await rig.wait_text(".conversation-column .transcript", "Identity reports checked.")
            conv = rig.core.order[0]
            agents = rig.core._agents_json(conv)
            assert [(a["fusionIdentity"], a["fusionModel"]) for a in agents] == [(identity, model) for identity in ("Coder", "Runner", "Coder")]
            graph = ".conversation-rail .agent-flow"
            nest = f'{graph} [data-id="group:Coder"] button'
            await rig.wait_for(nest)
            assert await rig.text(nest + " .agent-flow-name") == "Coder"
            assert "2 位" in await rig.text(nest + " .agent-flow-model")
            assert await rig.text(graph + " .react-flow__node-dragon button .agent-flow-model") == "Runner"
            assert await rig.count(graph + " .react-flow__node") == 3  # One root, one nest, one actual dragon.
            await rig.click(nest)
            await rig.wait_for(graph + " .react-flow__node", 5)
            assert not await rig.exists(".agentpanel")
            await rig.click_text(graph + " .react-flow__node-dragon button", "Coder-Login")
            await rig.wait_text(".agentpanel-run", "Login expert result.")
        finally:
            await rig.finish()

    run(loop, scenario())
