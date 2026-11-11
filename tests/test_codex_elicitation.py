"""An MCP server asking the user for something through Codex (``mcpServer/elicitation/request``): shown, answered, declined."""

from __future__ import annotations

import pytest

from desktop.core.codex.elicit import ALLOW, ALWAYS, DENY, NO, OPEN, SESSION, YES, plan_of, reply_for
from desktop.core.session_manager import SessionState
from desktop.core.transcript import PromptStatus, ToolStatus
from tests.codex_support import make_engine, run, until
from tests.fake_codex import ELICITATIONS


def params(kind, **more):
    return {"serverName": "notes", "threadId": "t", **ELICITATIONS[kind], **more}


# ----------------------------------------------------------------------------- reading the request


def test_a_form_becomes_one_question_per_field_with_the_kind_of_answer_each_takes():
    plan = plan_of(params("form"))
    assert plan.mode == "form" and [f.key for f in plan.fields] == ["title", "pages", "urgent", "place", "tags"]
    title, pages, urgent, place, tags = plan.views
    assert title.question == "Where should the note go?\nTitle（A short title）" and title.topic == "notes" and title.options == ()
    assert (pages.question, pages.options) == ("Pages", ()) and (urgent.options, place.options) == ((YES, NO), ("Inbox", "Archive"))
    assert tags.options == ("Alpha", "Beta") and tags.multi_select and not place.multi_select
    assert [f.required for f in plan.fields] == [True, False, False, False, False]


def test_a_request_without_fields_is_a_yes_or_no_and_a_link_is_a_question_about_opening_it():
    confirm = plan_of(params("confirm"))
    assert confirm.mode == "confirm" and confirm.views[0].question == "Allow notes to save this note?" and confirm.views[0].options == (ALLOW, DENY)
    bare = plan_of({"serverName": "notes", "mode": "form", "message": ""})
    assert bare.mode == "confirm" and "notes" in bare.views[0].question
    link = plan_of(params("url"))
    assert link.mode == "url" and link.url == "https://example.com/signin" and link.views[0].options == (OPEN, DENY)
    assert link.views[0].question == "Sign in to notes first.\nhttps://example.com/signin"


def test_codex_asking_whether_a_tool_may_run_says_which_tool_what_for_and_with_what():
    plan = plan_of(params("approval"))
    assert plan.mode == "approval"
    (view,) = plan.views
    assert view.options == (ALLOW, SESSION, ALWAYS, DENY) and view.topic == "notes"
    assert view.question.splitlines() == ['Allow the notes MCP server to run tool "save"?', "Save a note.", "参数：title=Commit messages；kind=preference"]
    shown = params("approval", _meta={"codex_approval_kind": "mcp_tool_call", "persist": [], "tool_params_display": [{"name": "t", "display_name": "Title", "value": "x"}]})
    only = plan_of(shown).views[0]
    assert only.options == (ALLOW, DENY) and only.question.splitlines()[-1] == "参数：Title=x", "no choice of how long when Codex offers none"
    long = plan_of(params("approval", _meta={"codex_approval_kind": "mcp_tool_call", "tool_params": {"text": "x" * 2000}})).views[0].question
    assert len(long) < 760 and long.endswith("…")


def test_the_choice_of_how_long_to_allow_a_tool_goes_back_in_the_form_codex_took_it_in():
    plan = plan_of(params("approval"))
    assert reply_for(plan, [[ALLOW]]) == {"action": "accept", "content": None, "_meta": None}
    assert reply_for(plan, [[SESSION]]) == {"action": "accept", "content": None, "_meta": {"persist": "session"}}
    assert reply_for(plan, [[ALWAYS]]) == {"action": "accept", "content": None, "_meta": {"persist": "always"}}
    assert reply_for(plan, [[DENY]]) == {"action": "decline", "content": None, "_meta": None}
    assert reply_for(plan, [["something else"]])["action"] == "decline", "only a clear yes allows"
    assert reply_for(plan, None)["action"] == "cancel"


@pytest.mark.parametrize(
    "request_params",
    [
        params("odd"),
        params("url", url="file:///C:/Windows/system32/calc.exe"),
        params("url", url="javascript:alert(1)"),
        params("form", requestedSchema="nonsense"),
        params("form", requestedSchema={"type": "object", "properties": {"x": {"type": "object"}}}),
        params("form", requestedSchema={"type": "object", "properties": {f"f{n}": {"type": "string"} for n in range(13)}}),
        {"serverName": "notes"},
    ],
)
def test_what_cannot_be_shown_is_not_planned(request_params):
    assert plan_of(request_params) is None


def test_the_openai_form_modes_are_read_like_a_form():
    for mode in ("openai/form", "openaiForm"):
        plan = plan_of(params("form", mode=mode))
        assert plan is not None and plan.mode == "form" and len(plan.fields) == 5


def test_choices_may_come_as_a_plain_enum_with_or_without_names():
    schema = {"type": "object", "properties": {
        "a": {"type": "string", "enum": ["x", "y"]},
        "b": {"type": "string", "enum": ["p", "q"], "enumNames": ["Pee", "Queue"]},
        "c": {"type": "array", "items": {"type": "string", "enum": ["m", "n"]}},
    }}
    plan = plan_of(params("form", requestedSchema=schema))
    assert [v.options for v in plan.views] == [("x", "y"), ("Pee", "Queue"), ("m", "n")]
    assert reply_for(plan, [["y"], ["Queue"], ["m", "n"]])["content"] == {"a": "y", "b": "q", "c": ["m", "n"]}


# ----------------------------------------------------------------------------- the reply


def test_the_answers_become_values_of_the_right_type():
    plan = plan_of(params("form"))
    reply = reply_for(plan, [["Buy milk"], ["12"], [YES], ["Archive"], ["Alpha", "Beta"]])
    assert reply == {"action": "accept", "content": {"title": "Buy milk", "pages": 12, "urgent": True, "place": "archive", "tags": ["a", "b"]}, "_meta": None}
    only_required = reply_for(plan, [["Just a title"], [""], [], [], []])
    assert only_required["content"] == {"title": "Just a title"}, "empty optional fields are left out"
    assert reply_for(plan, [["x"], ["3"], [NO], ["Inbox"], ["Beta"]])["content"] == {"title": "x", "pages": 3, "urgent": False, "place": "inbox", "tags": ["b"]}


def test_a_missing_required_field_or_a_number_that_is_not_one_declines():
    plan = plan_of(params("form"))
    assert reply_for(plan, [[""], ["1"], [YES], ["Inbox"], []])["action"] == "decline"
    assert reply_for(plan, [["ok"], ["many"], [YES], ["Inbox"], []])["action"] == "decline"
    assert reply_for(plan, [["ok"], ["1"], ["maybe"], ["Inbox"], []])["action"] == "decline"


def test_a_yes_or_no_and_a_link_are_accepted_or_declined_and_a_skip_is_a_cancel():
    confirm, link = plan_of(params("confirm")), plan_of(params("url"))
    assert reply_for(confirm, [[ALLOW]]) == {"action": "accept", "content": {}, "_meta": None}
    assert reply_for(confirm, [[DENY]]) == {"action": "decline", "content": None, "_meta": None}
    assert reply_for(link, [[OPEN]])["action"] == "accept" and reply_for(link, [[DENY]])["action"] == "decline"
    for plan in (confirm, link, plan_of(params("form"))):
        assert reply_for(plan, None) == {"action": "cancel", "content": None, "_meta": None}


def test_numbers_keep_their_kind():
    schema = {"type": "object", "properties": {"n": {"type": "number"}, "i": {"type": "integer"}}}
    plan = plan_of(params("form", requestedSchema=schema))
    assert reply_for(plan, [["2"], ["2"]])["content"] == {"n": 2, "i": 2}
    assert reply_for(plan, [["2.5"], ["2"]])["content"] == {"n": 2.5, "i": 2}
    assert reply_for(plan, [["2.0"], ["x"]])["action"] == "decline" and reply_for(plan, [["2.0"], ["3"]])["content"]["n"] == 2.0


# ----------------------------------------------------------------------------- against the app-server


async def opened(harness, project):
    controller = await harness.engine.open_new(project)
    assert controller.state is SessionState.READY, controller.last_error
    return controller


async def pending(controller):
    await until(lambda: controller.transcript.pending_prompts())
    (prompt,) = controller.transcript.pending_prompts()
    return prompt


def test_a_form_from_an_mcp_server_is_shown_as_questions_and_the_answers_go_back(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[elicit-form] go")
        prompt = await pending(controller)
        assert len(prompt.questions) == 5 and prompt.questions[0].topic == "notes" and prompt.questions[3].options == ("Inbox", "Archive")
        (card,) = [t for key, t in controller.transcript.turns[-1].tools.items() if key.startswith("elicitation-")]
        assert card.status is ToolStatus.AWAITING_PERMISSION, "the card waits for the user"
        assert controller.answer_question(prompt.id, ["Buy milk", "4", YES, "Archive", ["Beta"]])
        await until(lambda: not controller.busy)
        turn = controller.transcript.turns[-1]
        assert turn.blocks[-1].text == 'elicitation: {"_meta": null, "action": "accept", "content": {"pages": 4, "place": "archive", "tags": ["b"], "title": "Buy milk", "urgent": true}}'
        assert prompt.status is PromptStatus.ANSWERED
        assert any(t.status is ToolStatus.DONE and t.result == "已回答。" for t in turn.tools.values())
        await harness.engine.close_all()

    run(scenario())


def test_a_yes_or_no_from_an_mcp_server_can_be_allowed_or_refused(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        for choice, action in ((ALLOW, "accept"), (DENY, "decline")):
            assert controller.send_prompt("[elicit-confirm] go")
            prompt = await pending(controller)
            assert prompt.questions[0].options == (ALLOW, DENY)
            assert controller.answer_question(prompt.id, [choice])
            await until(lambda: not controller.busy)
            assert f'"action": "{action}"' in controller.transcript.turns[-1].blocks[-1].text
        await harness.engine.close_all()

    run(scenario())


def test_codex_asking_whether_an_mcp_tool_may_run_gets_the_users_choice_including_for_how_long(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        for choice, expected in ((ALLOW, '"_meta": null'), (SESSION, '"_meta": {"persist": "session"}'), (DENY, '"action": "decline"')):
            assert controller.send_prompt("[elicit-approval] go")
            prompt = await pending(controller)
            assert prompt.questions[0].options == (ALLOW, SESSION, ALWAYS, DENY) and "Commit messages" in prompt.questions[0].question
            assert controller.answer_question(prompt.id, [choice])
            await until(lambda: not controller.busy)
            assert expected in controller.transcript.turns[-1].blocks[-1].text
        await harness.engine.close_all()

    run(scenario())


def test_skipping_the_question_cancels_it(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[elicit-confirm] go")
        prompt = await pending(controller)
        assert controller.answer_question(prompt.id, None)
        await until(lambda: not controller.busy)
        turn = controller.transcript.turns[-1]
        assert '"action": "cancel"' in turn.blocks[-1].text and any(t.result == "已跳过。" for t in turn.tools.values())
        await harness.engine.close_all()

    run(scenario())


def test_a_link_is_opened_in_the_browser_only_when_the_user_agrees(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[elicit-url] go")
        prompt = await pending(controller)
        assert controller.answer_question(prompt.id, [DENY])
        await until(lambda: not controller.busy)
        assert harness.opened == [], "declined: nothing was opened"
        assert controller.send_prompt("[elicit-url] again")
        prompt = await pending(controller)
        assert controller.answer_question(prompt.id, [OPEN])
        await until(lambda: not controller.busy)
        assert harness.opened == ["https://example.com/signin"] and '"action": "accept"' in controller.transcript.turns[-1].blocks[-1].text
        await harness.engine.close_all()

    run(scenario())


def test_a_request_of_a_kind_nobody_can_show_is_declined_and_the_user_is_told(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[elicit-odd] go")
        await until(lambda: not controller.busy)
        turn = controller.transcript.turns[-1]
        assert '"action": "decline"' in turn.blocks[-1].text and not turn.pending_prompts()
        notices = [getattr(b, "text", "") for b in turn.blocks if type(b).__name__ == "NoticeBlock"]
        assert any("notes" in n and "mystery" in n for n in notices)
        await harness.engine.close_all()

    run(scenario())


def test_a_question_left_open_when_the_turn_is_stopped_is_cancelled_and_does_not_linger(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[elicit-form] go")
        prompt = await pending(controller)
        await controller.interrupt()
        await until(lambda: not controller.busy, 15)
        assert prompt.status is not PromptStatus.PENDING and controller._pending == {} and controller._requests == {}
        await harness.engine.close_all()

    run(scenario())
