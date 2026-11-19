import { crc32, deflateSync } from "node:zlib";

const hex = (bytes) => Buffer.from(bytes.replace(/\s+/g, ""), "hex");
const u32be = (...ints) => { const b = Buffer.alloc(4 * ints.length); ints.forEach((n, i) => b.writeUInt32BE(n, i * 4)); return b; };
const u32le = (...ints) => { const b = Buffer.alloc(4 * ints.length); ints.forEach((n, i) => b.writeUInt32LE(n, i * 4)); return b; };
const pngChunk = (kind, data = Buffer.alloc(0)) => {
  const body = Buffer.concat([Buffer.from(kind), data]);
  return Buffer.concat([u32be(data.length), body, u32be(crc32(body))]);
};
const riffChunk = (kind, data) => Buffer.concat([Buffer.from(kind), u32le(data.length), data, Buffer.alloc(data.length % 2)]);

export function pictureFixtures() {
  const gif = hex(`47494638396101000100800000ff00000000ff
    21ff0b4e45545343415045322e300301000000
    21f9040014000000 2c000000000100010000 0202440100
    21f9040014000000 2c000000000100010000 02024c0100 3b`);
  const red = deflateSync(hex("00ff0000ff")), blue = deflateSync(hex("000000ffff"));
  const fc = (i) => pngChunk("fcTL", Buffer.concat([u32be(i, 1, 1, 0, 0), hex("000100050000")]));
  const apng = Buffer.concat([hex("89504e470d0a1a0a"), pngChunk("IHDR", Buffer.concat([u32be(1, 1), hex("0806000000")])),
    pngChunk("acTL", u32be(2, 0)), fc(0), pngChunk("IDAT", red), fc(1), pngChunk("fdAT", Buffer.concat([u32be(2), blue])), pngChunk("IEND")]);
  // Two tiny lossless VP8L frames, 2×2 red and blue; wrap them in a new WebP animation.
  const anmf = (pixels) => riffChunk("ANMF", Buffer.concat([hex("000000000000010000010000c8000000"), riffChunk("VP8L", hex(pixels))]));
  const webpBody = Buffer.concat([Buffer.from("WEBP"), riffChunk("VP8X", hex("02000000010000010000")), riffChunk("ANIM", hex("000000000000")),
    anmf("2f014000000710fd8ffe0722a2ff01"), anmf("2f014000000710d1fffe0722a2ff01")]);
  const webp = Buffer.concat([Buffer.from("RIFF"), u32le(webpBody.length), webpBody]);
  return Object.fromEntries([["gif", gif], ["apng", apng], ["webp", webp]].map(([suffix, bytes]) => [
    `sample.${suffix}`, { kind: "image", size: bytes.length, url: `data:image/${suffix};base64,${bytes.toString("base64")}` },
  ]));
}

export async function checkInspector({ page, ok, clickText, calls }) {
  const selector = ".inspector-splitter";
  const width = (panel = ".filepanel") => page.eval(`Math.round(document.querySelector('${panel}').getBoundingClientRect().width)`);
  const settings = async () => (await calls("settings.set")).filter((r) => r.key === "inspectorWidth");
  const dragFrom = async (delta) => {
    const r = await page.rect(selector);
    const x = r.x + r.w / 2, y = r.y + r.h / 2;
    await page.mouse("mousePressed", x, y);
    await page.mouse("mouseMoved", x + delta, y, { buttons: 1 });
    return () => page.mouse("mouseReleased", x + delta, y);
  };
  await clickText(".text .pathref", "sample.gif");
  const initial = await width();
  const before = (await settings()).length;
  const release = await dragFrom(-60);
  await page.waitFor(`document.querySelector('.filepanel').offsetWidth === ${initial + 60}`);
  ok("Inspector dragging changes the live width without saving", (await settings()).length === before);
  await release();
  await page.waitFor(`__fake.callsOf('settings.set').some(r => r.key === 'inspectorWidth' && r.value === ${initial + 60})`);
  await page.key("Escape");
  await clickText(".text .pathref", "sample.gif");
  await page.waitFor(`document.querySelector('.filepanel').offsetWidth === ${initial + 60}`);
  ok("The inspector keeps the width after closing and reopening", true);
  const again = await dragFrom(90);
  await page.waitFor(`document.querySelector('.filepanel').offsetWidth === ${initial - 30}`);
  await page.key("Escape");
  await again();
  ok("Escape cancels the drag, not the panel", await width() === initial + 60 && (await settings()).length === before + 1);
  await page.key("Home", { vk: 36 });
  ok("Inspector Home respects the 340px minimum", await width() === 340);
  await page.key("End", { vk: 35 });
  const max = await width();
  ok("Inspector End leaves 360px for the conversation", await page.eval("document.querySelector('.app > .main').offsetWidth >= 360"));
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
  await page.waitFor(`document.querySelector('.filepanel').offsetWidth < ${max} && document.querySelector('.app > .main').offsetWidth >= 360`);
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 900, deviceScaleFactor: 1, mobile: false });
  ok("The narrow layout keeps the original overlay without a drag handle", await page.eval("getComputedStyle(document.querySelector('.inspector-splitter')).display === 'none' && getComputedStyle(document.querySelector('.filepanel')).position === 'absolute'"));
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await page.key("Escape");
  const id = await page.eval("__fake.events.findLast(([name, p]) => name === 'tabs' && p.select)[1].select");
  const agent = { id: "preview-width", name: "样例专家", role: "工作专家", model: "GPT-6.1-Sol", effort: "high", task: "核对样例。", status: "working", startedAt: Date.now() - 1000, tokens: null };
  const patch = (agents) => page.eval(`__fake.patch(${JSON.stringify(id)}, ${JSON.stringify({ agents })})`);
  await patch([agent]);
  await page.click("#conversation-team .agent-chip");
  await page.waitFor("!!document.querySelector('.agentpanel')");
  const agentWidth = await width(".agentpanel");
  const drop = await dragFrom(45);
  await drop();
  await page.waitFor(`document.querySelector('.agentpanel').offsetWidth === ${max - 45}`);
  ok("The Sub-Agent panel uses the same saved width and drag handle", agentWidth === max);
  await page.key("Escape");
  await patch([]);
}

export async function checkVideo({ page, ok, clickText, calls }) {
  const video = (action) => page.eval(`__fake.callsOf('files.video').filter(p => p.action === ${JSON.stringify(action)}).at(-1)`);
  const open = (name) => clickText(".text .pathref", name);
  const src = () => page.eval("document.querySelector('.fp-video-stage img').src");
  const before = page.errorTexts().length;
  await open("sample.mp4");
  await page.waitFor("document.querySelector('.fp-video-stage img')?.naturalWidth === 96");
  const first = await video("open"), poster = await src();
  ok("Video opens paused with a JPEG, aspect ratio, size, codec, and duration", first.path === "sample.mp4" && first.width > 0 && first.width <= 1280 &&
    await page.eval("document.querySelector('.fp-video-play').getAttribute('aria-label') === '播放' && document.querySelector('.fp-video-stage img').height > 0 && document.querySelector('.fp-facts').textContent.includes('96 × 54 · H264 / AAC · 0:42')"));
  await page.click(".fp-video-play");
  await page.waitFor("document.querySelector('.fp-video-play').getAttribute('aria-label') === '暂停'");
  ok("Play reaches Python and replaces the paused frame", (await video("play")).token === first.token && await src() !== poster && await page.eval("document.querySelector('.fp-video-clock').textContent === '0:01 / 0:42'"));
  await page.click(".fp-video-stage");
  await page.key(" ", { code: "Space", vk: 32 });
  await page.waitFor("document.querySelector('.fp-video-play').getAttribute('aria-label') === '播放'");
  ok("Space pauses when the video stage has focus", (await video("pause"))?.token === first.token);
  await page.click(".fp-video-time");
  await page.key("End", { vk: 35 });
  await page.waitFor("document.querySelector('.fp-video-clock').textContent === '0:42 / 0:42'");
  ok("Seek sends milliseconds and the clock follows the event", (await video("seek")).position === 42000);
  await page.click(".fp-video-mute");
  ok("Mute and volume use the dedicated playback control", (await video("volume")).muted === true && (await video("volume")).volume === .7);
  await open("sample.webm");
  await page.waitFor("document.querySelector('.fp-video-stage img')?.naturalWidth === 96");
  const second = await video("open"), secondFrame = await src();
  await page.eval(`__fake.emit('files.video', ${JSON.stringify({ token: first.token, url: poster, position: 9999, playing: true, ended: false })})`);
  await page.wait(100);
  ok("A replaced video is closed and its late frames cannot land in the new player", second.token !== first.token && (await video("close")).token === first.token && await src() === secondFrame);
  await page.key("Escape");
  await page.waitFor(`__fake.callsOf('files.video').some(p => p.action === 'close' && p.token === ${JSON.stringify(second.token)})`);
  ok("Closing the file panel unloads the video", await page.eval("!document.querySelector('.filepanel')"));
  await open("broken.mov");
  await page.waitFor("!!document.querySelector('.fp-error')");
  ok("A failed decoder leaves a Chinese sentence and the existing actions", await page.eval("document.querySelector('.fp-error').textContent.includes('无法解码') && document.querySelectorAll('.fp-actions button').length === 2"));
  ok("JPEG video streaming triggers no console or CSP errors", page.errorTexts().length === before, page.errorTexts());
  const opens = (await calls("files.video")).filter((request) => request.action === "open");
  ok("Each video requests one stream, not the whole file", opens.length === 3 && !(await calls("files.page")).some((p) => /\.(mp4|webm|mov)$/.test(p.path)));
}

export async function checkPictures({ page, ok, clickText }) {
  for (const [name, wide] of [["sample.gif", 1], ["sample.apng", 1], ["sample.webp", 2]]) {
    await clickText(".text .pathref", name);
    await page.waitFor(`document.querySelector('.fp-picture img')?.naturalWidth === ${wide}`);
    const mime = `data:image/${name.split(".")[1]};base64,`;
    ok(`${name}: animated fixture is drawn from its original bytes`, await page.eval(`document.querySelector('.fp-picture img').src.startsWith(${JSON.stringify(mime)})`));
  }
}
