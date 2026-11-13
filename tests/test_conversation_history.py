"""The closed history of an engine switch is durable data, not executable serialization or a disposable cache."""

from __future__ import annotations

import dataclasses
import json

import pytest

from desktop.core.attachments import image_attachment
from desktop.core.conversation_history import ConversationHistory, prepend
from desktop.core.handoff import build_handoff
from desktop.core.settings import settings_from_dict, settings_to_dict
from desktop.core.transcript import Transcript, TurnStatus
from desktop.core.viewmodel import turn_json
from tests.support import tiny_png


def test_history_round_trips_tools_plans_attachments_and_handoff_without_id_collisions(tmp_path):
    transcript = Transcript()
    turn = transcript.start_turn("检查图示", (image_attachment("diagram.png", tiny_png(), "image/png"),))
    transcript.append_thinking(turn, "think", 0, "先看输入边界。")
    transcript.end_text(turn, "answer", 0, "补充校验。")
    item = transcript.ensure_tool(turn, "inspect-input", "read_file")
    transcript.set_tool_input(turn, item, {"file_path": "inventory.py"}, final=True)
    transcript.record_result(turn, item, "输入校验已检查。", False)
    transcript.set_plan(turn, [{"content": "检查输入", "status": "completed"}, {"content": "补测试", "status": "in_progress"}])
    transcript.finish_turn(turn, TurnStatus.DONE)
    original = turn_json(turn, lambda _: None)
    history = ConversationHistory(tmp_path)
    history.save("dv:segment", {"turns": transcript.turns, "ledger": {}, "handoff": build_handoff(transcript.turns)})
    loaded = history.load("dv:segment")
    assert turn_json(loaded["turns"][0], lambda _: None) == original
    assert loaded["turns"][0].user_block.attachments[0].data == tiny_png()
    current = Transcript()
    live = current.start_turn("继续")
    prepend(current, loaded["turns"], "dv:segment")
    prepend(current, loaded["turns"], "dv:segment")
    assert len(current.turns) == 2 and current.turns[-1] is live
    assert len({t.id for t in current.turns}) == 2
    attachment = current.turns[0].user_block.attachments[0]
    assert current.attachment(attachment.id) is attachment
    assert current.active_turn is live


def test_unknown_history_types_are_rejected_without_importing_or_constructing_them(tmp_path):
    history = ConversationHistory(tmp_path)
    history.save("cg:segment", {"turns": []})
    path = history._path("cg:segment")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["data"] = {"node": "subprocess.Popen", "fields": {"args": ["unused"]}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        history.load("cg:segment")


def test_title_and_segment_link_settings_round_trip_and_reject_cycles():
    settings = settings_from_dict({"session_links": {"cg:a": "dv:b", "dv:b": "cg:a", "fy:c": "cg:d", "bad": "cg:x", "cg:self": "cg:self", "cg:list": []}, "session_auto_titles": {"cg:d": "  库存  校验  ", "bad": 1}})
    assert settings.session_links == {"fy:c": "cg:d"}
    assert settings.session_auto_titles == {"cg:d": "库存 校验"}
    assert settings_from_dict(settings_to_dict(settings)) == settings
