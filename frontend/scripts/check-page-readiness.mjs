// Exercise the CDP driver against a genuinely slow first render, beyond the old 1.2-second sleep.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import { openPage } from "./cdp.mjs";

const server = createServer((_request, response) => {
  response.setHeader("Content-Type", "text/html; charset=utf-8");
  response.end(`<div id="root"></div><script>setTimeout(() => document.getElementById('root').innerHTML = '<div class="app"><textarea class="input"></textarea><button class="newbtn">New</button></div>', 2300)</script>`);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
let first;
let second;
try {
  const start = Date.now();
  first = await openPage(`http://127.0.0.1:${server.address().port}`);
  assert(Date.now() - start >= 2300);
  await first.click(".newbtn");
  // Concurrent browsers must not borrow another process's CDP port or target.
  second = await openPage("data:text/html,<title>isolated</title><p>ready</p>");
  assert.equal(await second.eval("document.title"), "isolated");
  assert.equal(await first.eval("document.querySelector('.newbtn').textContent"), "New");
  console.log("PASS slow first render and isolated CDP endpoints");
  const worker = spawnSync(process.execPath, ["--input-type=module", "-e", `import { openPage } from ${JSON.stringify(new URL("./cdp.mjs", import.meta.url).href)}; const page = await openPage('data:text/html,<title>cleanup</title>'); console.log('owned-browser:' + page.browserPid); throw new Error('expected check failure');`], { encoding: "utf8", timeout: 20000, windowsHide: true });
  assert.equal(worker.status, 1, worker.stderr);
  const pid = Number(worker.stdout.match(/owned-browser:([0-9]+)/)?.[1]);
  assert(pid > 0, worker.stdout);
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" }, "the failed check must close its own browser");
  assert.equal(await first.eval("document.querySelector('.newbtn').textContent"), "New", "the other check's browser must stay alive");
  console.log("PASS error exits clean only their own browser process tree");
} finally {
  await second?.close();
  await first?.close();
  await new Promise((resolve) => server.close(resolve));
}
