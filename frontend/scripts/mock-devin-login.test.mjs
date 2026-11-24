import assert from "node:assert/strict";
import test from "node:test";
import { mockBridge } from "../src/mock/bridge.ts";

test("cancelled Devin login stays signed out without a retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { devin } = mockBridge;
  const login = devin.login();
  const cancelled = await devin.cancelLogin();
  t.mock.timers.runAll();
  assert.equal(cancelled.auth, "signed-out");
  assert.deepEqual(await login, cancelled);
  assert.deepEqual(await devin.account(), cancelled);
});

for (const staleFirst of [true, false]) {
  test(`cancelled Devin login cannot ${staleFirst ? "finish a pending retry" : "re-enable models after a retry"}`, async (t) => {
    const timers = [];
    t.mock.method(globalThis, "setTimeout", (finish) => timers.push(finish));
    const { devin } = mockBridge;
    const stale = devin.login();
    assert.equal((await devin.cancelLogin()).auth, "signed-out");
    const retry = devin.login();
    const [finishStale, finishRetry] = timers;

    const checkStale = async (expected) => {
      finishStale();
      assert.deepEqual(await stale, expected);
      assert.deepEqual(await devin.account(), expected);
    };
    const pending = await devin.account();
    assert.equal(pending.auth, "signing-in");
    if (staleFirst) await checkStale(pending);

    finishRetry();
    assert.equal((await retry).auth, "signed-in");
    assert.equal((await devin.account()).enabled, true);
    const hidden = await devin.setEnabled(false);
    assert.equal(hidden.enabled, false);
    if (!staleFirst) await checkStale(hidden);
    assert.equal((await devin.setEnabled(true)).enabled, true);
  });
}
