"""MCP-style forms must not repeat the same question from their message and schema."""

from __future__ import annotations

import pytest

from desktop.core.elicit import plan_of, reply_for


def form(message, properties):
    return plan_of({
        "mode": "form", "serverName": "Devin", "message": message,
        "requestedSchema": {"type": "object", "properties": properties, "required": list(properties)},
    })


@pytest.mark.parametrize("description", ["如何处理这份演示配置？", " \n如何处理这份演示配置？ \t"])
def test_a_question_repeated_in_the_message_and_description_is_shown_once(description):
    question = "如何处理这份演示配置？"
    plan = form(question, {"config": {"type": "string", "title": "配置文件", "description": description, "enum": ["保留", "移除"]}})
    assert plan is not None
    (view,) = plan.views
    assert view.topic == "配置文件"
    assert view.question == question
    assert view.options == ("保留", "移除")
    assert reply_for(plan, [["保留"]])["content"] == {"config": "保留"}


def test_a_question_repeated_in_the_message_title_and_description_is_shown_once():
    question = "采用哪种方案？"
    plan = form(question, {"choice": {"type": "string", "title": question, "description": question}})
    assert plan is not None
    assert plan.views[0].question == question
    assert plan.views[0].topic == "", "a full question is not repeated as its own topic"


def test_distinct_form_instructions_and_field_details_are_not_lost():
    plan = form("请选择配置。", {
        "approach": {"type": "string", "title": "方案", "description": "决定实施顺序。", "enum": ["先测试", "先实现"]},
        "extras": {"type": "array", "title": "附加项", "description": "可以多选。", "items": {"type": "string", "enum": ["文档", "示例"]}},
    })
    assert plan is not None
    first, second = plan.views
    assert first.question == "请选择配置。\n方案（决定实施顺序。）"
    assert second.question == "附加项（可以多选。）" and second.multi_select
    assert reply_for(plan, [["先测试"], ["文档", "示例"]])["content"] == {"approach": "先测试", "extras": ["文档", "示例"]}


def test_whitespace_separating_words_is_not_deleted_when_comparing_questions():
    plan = form("Keep a b", {"choice": {"type": "string", "title": "Names", "description": "Keep ab"}})
    assert plan is not None
    assert "Keep a b" in plan.views[0].question and "Keep ab" in plan.views[0].question


def test_related_but_different_instructions_are_not_mistaken_for_duplicates():
    plan = form("请选择配置。", {"choice": {"type": "string", "title": "配置", "description": "请选择配置。保留备份。"}})
    assert plan is not None
    assert "保留备份。" in plan.views[0].question
