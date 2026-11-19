"""Bounded picture history, restart, estimates, and compaction."""
import asyncio
import base64
import json

from desktop.core.chatgpt.responses import call_output, for_responses, user_message
from desktop.core.harness import compact
from desktop.core.harness.history import EXPIRED_TOOL_IMAGES, NO_TOOL_IMAGES
from desktop.core.harness.store import SavedSession, SavedTurn, SessionStore
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, calls, say
from tests.test_harness_loop import Script
from tests.tool_image_support import png


def image_output(index):
    data = base64.b64encode(png(width=index + 1)).decode("ascii")
    return {**call_output(f"c{index}", f"picture-{index}.png (image/png)"), "wc_images": [{"media_type": "image/png", "data": data}]}


def image_parts(items):
    return [part["image_url"] for item in items for part in item.get("content", ()) if part.get("type") == "input_image"]


def test_store_and_wire_drop_the_ninth_oldest_picture_without_adding_history_items(tmp_path):
    pictures = [image_output(index) for index in range(9)]
    items = [user_message("Inspect the pictures."), *pictures]
    store = SessionStore(tmp_path)
    session = SavedSession("tool-images", "project", "Example", "model", "", "code", items=items,
                           turns=[SavedTurn(0, 1.0, 2.0)])
    store.save(session)
    saved = store.load(session.id)
    assert saved is not None and saved.turns == session.turns
    assert len(saved.items) == len(items) and saved.items[2:] == pictures[1:]
    raw = (tmp_path / "sessions" / "tool-images.json").read_text(encoding="utf-8")
    assert pictures[0]["wc_images"][0]["data"] not in raw
    assert sum("wc_images" in item for item in saved.items) == 8
    assert saved.items[1] == call_output("c0", pictures[0]["output"] + "\n" + EXPIRED_TOOL_IMAGES)
    sent = for_responses(items)
    assert sent[1] == saved.items[1]
    assert image_parts(sent) == ["data:image/png;base64," + item["wc_images"][0]["data"] for item in pictures[1:]]
    assert "wc_images" in pictures[0] and EXPIRED_TOOL_IMAGES not in pictures[0]["output"]


def test_tool_picture_estimate_equals_a_user_picture_estimate_regardless_of_payload_length():
    item = image_output(0)
    item["wc_images"][0]["data"] = "A" * 500_000
    plain = call_output(item["call_id"], item["output"])
    user_text = user_message("look")
    user_picture = user_message("look", ["data:image/png;base64,AAAA"])
    assert compact.estimate_item(item) - compact.estimate_item(plain) == compact.estimate_item(user_picture) - compact.estimate_item(user_text) == compact.IMAGE_TOKENS


def test_compaction_drops_covered_tool_picture_bytes_but_keeps_unsummarised_tail():
    early, late = image_output(0), image_output(1)
    fc = {"type": "function_call", "name": "read_file", "arguments": "{}"}
    view = [user_message("Inspect those pictures."), {**fc, "call_id": "c0"}, early,
            user_message("What about this other one?"), {**fc, "call_id": "c1"}, late]
    script = Script(say("The first picture was inspected."))
    result = asyncio.run(compact.compact(view, stream=script, model="model", reasoning=None, window=10_000,
                                        prefix_request={"model": "model", "instructions": "original instructions"}))
    assert result is not None and result.cut == 3
    [request] = script.requests
    rendered = json.dumps(request["input"])
    assert "TOOL RESULT: picture-0.png (image/png)" in rendered and "[1 image(s) attached]" in rendered
    assert "wc_images" not in rendered and early["wc_images"][0]["data"] not in rendered
    assert image_parts(for_responses(result.view)) == ["data:image/png;base64," + late["wc_images"][0]["data"]]


def test_own_loop_prunes_when_ninth_picture_arrives_and_refreshes_flag_on_model_change(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        models = [{"slug": "vision", "input_modalities": ["text", "image"]}, {"slug": "text-only", "input_modalities": ["text"]}]
        for index in range(9):
            (project / f"picture-{index}.png").write_bytes(png(width=index + 1))
        with FakeResponses(models=models, token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake)
            await engine.list_models()
            controller = await engine.open_new(project, model="vision", autonomy="code")
            try:
                fake.script(calls([("read_file", json.dumps({"file_path": f"picture-{i}.png"}), f"c{i}", f"fc-{i}") for i in range(8)]),
                            call("read_file", {"file_path": "picture-8.png"}, call_id="c8"), say("Read the pictures."))
                assert controller.send_prompt("Inspect the pictures.")
                await finish_turn(controller)
                assert controller.saved.turns[-1].status == "done"
                first_batch = image_parts(fake.requests[1]["input"])
                last_batch = image_parts(fake.requests[2]["input"])
                assert len(first_batch) == len(last_batch) == 8
                assert first_batch[1:] == last_batch[:-1] and first_batch[0] not in last_batch
                [first_result] = [item for item in controller.saved.items if item.get("call_id") == "c0" and item.get("type") == "function_call_output"]
                assert "wc_images" not in first_result and EXPIRED_TOOL_IMAGES in first_result["output"]
                loaded = engine.sessions.load(controller.session_id)
                assert loaded is not None and loaded.items == controller.saved.items
                assert sum(compact.is_user(item) for item in loaded.items) == 1
                assert await controller.update_settings(model="text-only")
                fake.script(call("read_file", {"file_path": "picture-0.png"}, call_id="refused"), say("Cannot view it."))
                assert controller.send_prompt("Please check it again.")
                await finish_turn(controller)
                assert controller.saved.turns[-1].status == "done"
                assert not image_parts(fake.requests[-2]["input"]) and not image_parts(fake.requests[-1]["input"])
                assert NO_TOOL_IMAGES in json.dumps(fake.requests[-2]["input"])
                [refused] = [item for item in fake.requests[-1]["input"] if item.get("call_id") == "refused" and item.get("type") == "function_call_output"]
                assert "current model does not support images" in refused["output"]
            finally:
                await engine.close_all()
    asyncio.run(go())
