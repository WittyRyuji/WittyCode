import assert from "node:assert/strict";
import test from "node:test";
import { CALL_TIMEOUT_MS, CallError, QWebChannelTransport } from "../src/transport.ts";

test("model handoffs can outlive the RPC timeout, while ordinary settings retain it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const sent = [];
  let reply;
  const backend = {
    reply: { connect(callback) { reply = callback; } },
    event: { connect() {} },
    call(method, params, id, done) { sent.push({ method, params: JSON.parse(params), id }); done(); },
  };
  class Channel {
    constructor(_transport, ready) { ready({ objects: { backend } }); }
  }
  const transport = new QWebChannelTransport({}, Channel);
  const model = transport.call("session.choose", { id: "demo", field: "model", value: "fusion" });
  const main = transport.call("fusion.changeMain", { id: "demo", model: "chatgpt:demo" });
  const defaults = transport.call("session.choose", { field: "model", value: "chatgpt:demo" }).catch((error) => error);
  const reasoning = transport.call("session.choose", { id: "demo", field: "reasoning", value: "high" }).catch((error) => error);
  await Promise.resolve();
  t.mock.timers.tick(CALL_TIMEOUT_MS + 1);
  assert(await defaults instanceof CallError);
  assert(await reasoning instanceof CallError);
  reply(sent[0].id, true, '{"ok":true}');
  reply(sent[1].id, false, '"原模型压缩已停止，未切换。"');
  assert.deepEqual(await model, { ok: true });
  await assert.rejects(main, /未切换/);
});
