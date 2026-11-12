import assert from "node:assert/strict";
import test from "node:test";
import { activeTurnAt, turnLinks } from "../src/lib/turnNavigation.ts";

const turn = (id, blocks, status = "done") => ({ id, blocks, status, prompts: [] });
const user = (text, extra = {}) => ({ kind: "user", id: "u", text, ...extra });

test("turn navigation has one marker per turn and previews the original message, not its tools or steering", () => {
  const turns = [turn("a", [user("  检查\n布局  "), user("不要覆盖原消息", { steer: true }), { kind: "activity", id: "g", entries: [] }]), turn("b", [user("补测试")], "running")];
  assert.deepEqual(turnLinks(turns), [{ id: "a", label: "检查 布局", status: "done" }, { id: "b", label: "补测试", status: "running" }]);
  assert.deepEqual(turnLinks([]), []);
  assert.deepEqual(turnLinks([turn("other", [])]), [{ id: "other", label: "第 1 轮", status: "done" }]);
});

test("attachments and system-only turns have useful labels without assuming a user block exists", () => {
  const links = turnLinks([
    turn("a", [user("", { attachments: [{ name: "diagram.png" }] })]),
    turn("b", [{ kind: "switch", id: "s", to: { model: "demo-model" } }]),
    turn("c", [{ kind: "notice", id: "n", text: "已压缩上下文" }]),
    turn("d", [{ kind: "error", id: "e", message: "连接失败" }], "failed"),
    turn("e", [{ kind: "text", id: "t", markdown: "恢复的回答" }]),
  ]);
  assert.deepEqual(links.map((link) => link.label), ["diagram.png", "切换到 demo-model", "已压缩上下文", "连接失败", "恢复的回答"]);
  assert.equal(links[3].status, "failed");
});

test("long message previews stay bounded and never cut an emoji in half", () => {
  const label = turnLinks([turn("a", [user("😀".repeat(110))])])[0].label;
  assert.equal(Array.from(label).length, 101);
  assert.equal(label, "😀".repeat(100) + "…");
});

test("the active marker follows the reading line, exact boundaries and the bottom of a short final turn", () => {
  const tops = [28, 500, 1300];
  assert.equal(activeTurnAt([], 0), -1);
  assert.equal(activeTurnAt(tops, 0), 0);
  assert.equal(activeTurnAt(tops, 499), 0);
  assert.equal(activeTurnAt(tops, 500), 1);
  assert.equal(activeTurnAt(tops, 900), 1);
  assert.equal(activeTurnAt(tops, 2000), 2);
  assert.equal(activeTurnAt(tops, 900, true), 2);
  assert.equal(activeTurnAt([28], 0, true), 0);
});
