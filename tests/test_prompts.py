from __future__ import annotations

from droid_sdk import PermissionOption, PermissionRequest, QuestionRequest, ToolConfirmationOutcome

from desktop.core.prompts import UNSUPPORTED_OPTIONS, action_view, option_views, question_views
from desktop.devtools.replay import load_records, request_from_record
from tests.support import fixture_path


def first_request(fixture: str, kind: type, nth: int = 0):
    found = [
        request
        for request in (request_from_record(record) for record in load_records(fixture_path(fixture)) if record.get("source") == "handler")
        if isinstance(request, kind)
    ]
    return found[nth]


def test_permission_options_are_translated_and_toned():
    request = first_request("stream_turn.jsonl", PermissionRequest)
    views = option_views(request.options)
    values = [view.value for view in views]
    assert "proceed_once" in values and "cancel" in values
    by_value = {view.value: view for view in views}
    assert by_value["proceed_once"].label == "允许（仅此一次）"
    assert by_value["proceed_once"].tone == "primary"
    assert by_value["cancel"].label == "拒绝"
    assert by_value["cancel"].tone == "danger"
    assert not set(values) & UNSUPPORTED_OPTIONS


def test_proceed_edit_is_never_offered():
    options = [
        PermissionOption(label="Proceed once", value=ToolConfirmationOutcome.PROCEED_ONCE),
        PermissionOption(label="Edit the plan", value=ToolConfirmationOutcome.PROCEED_EDIT),
        PermissionOption(label="Cancel", value=ToolConfirmationOutcome.CANCEL),
    ]
    assert [view.value for view in option_views(options)] == ["proceed_once", "cancel"]


def test_create_file_action_shows_path_and_new_file_diff():
    request = first_request("stream_turn.jsonl", PermissionRequest)
    view = action_view(request.actions[0])
    assert view.kind == "create"
    assert view.title.startswith("创建文件 ")
    assert view.path and view.path.endswith("hello.txt")
    assert len(view.diffs) == 1 and view.diffs[0].is_new_file


def test_edit_action_shows_a_diff_between_old_and_new_content():
    request = first_request("stream_turn.jsonl", PermissionRequest, 1)
    view = action_view(request.actions[0])
    assert view.kind == "edit"
    assert view.title.startswith("编辑文件 ")
    (diff,) = view.diffs
    assert (diff.added, diff.removed) == (1, 1)


def test_execute_action_prefers_the_full_command():
    request = first_request("interrupt.jsonl", PermissionRequest)
    view = action_view(request.actions[0])
    assert view.kind == "execute"
    assert view.title == "运行命令"
    action = request.actions[0]
    assert view.command == (action.full_command or action.command)
    assert view.command


def test_question_views_keep_options_and_multi_select_flag():
    request = first_request("ask_user.jsonl", QuestionRequest)
    (view,) = question_views(request.questions)
    assert view.question == "Which greeting word do you prefer?"
    assert len(view.options) >= 2
    assert view.multi_select is False
    assert view.index == request.questions[0].index