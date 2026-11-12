// Stands in for the Python side in the design page: the same calls as `Bridge`, answered after a believable delay.

import type { Bridge, TestResult } from "../bridge";
import type { ApiProvider, ChatGPTAccount, DevinAccount, EnvState, ProviderDraft } from "../types";
import { envNameOf } from "../lib/models";
import { CHATGPT_ACCOUNT, DEVIN_ACCOUNT } from "./data";
import { mockImporter } from "./importer";
import { mockMemory } from "./memory";

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));

let devin: DevinAccount = { ...DEVIN_ACCOUNT };
let chatgpt: ChatGPTAccount = { ...CHATGPT_ACCOUNT };
let devinCancelled = false;
let chatgptCancelled = false;
let chatgptAttempt = 0;
let maximized = false;

/** The demo machine's environment: a few keys are there, one was set after WittyCode started, the rest are not set. */
const ENV: Record<string, EnvState> = { OPENROUTER_API_KEY: "set", ANTHROPIC_API_KEY: "set", OPENAI_API_KEY: "pending" };
const envState = (name: string): EnvState => ENV[name] ?? "unset";

/** What a server at this address would answer: the demo knows a few. */
function fakeServer(d: ProviderDraft): TestResult {
  if (d.kind !== "bedrock-converse" && !/^https?:\/\/.+/.test(d.baseUrl)) return { ok: false, text: "Base URL 要以 http:// 或 https:// 开头" };
  if (/localhost|127\.0\.0\.1/.test(d.baseUrl)) return { ok: false, text: `连不上 ${d.baseUrl.replace(/^https?:\/\//, "").replace(/\/.*$/, "")}：服务没有运行？` };
  if (d.keyMode === "env") {
    const name = envNameOf(d.keyHint);
    if (!name) return { ok: false, text: "环境变量名是空的" };
    if (envState(name) === "unset") return { ok: false, text: `环境变量 ${name} 没有设置，请求没有带密钥` };
    if (envState(name) === "pending") return { ok: false, text: `环境变量 ${name} 是在 WittyCode 启动之后设置的：重启 WittyCode 再试` };
  }
  const models = d.kind === "anthropic" ? ["claude-sonnet-4-5-20250929", "claude-opus-4-1-20250805"] : ["openai/gpt-oss-120b", "qwen/qwen3-coder", "deepseek/deepseek-v3.1", "moonshotai/kimi-k2"];
  return { ok: true, ms: 380, models, text: `连接成功 · 380 ms · ${models.length} 个模型` };
}

export const mockBridge: Bridge = {
  devin: {
    account: async () => ({ ...devin }),
    async login() {
      devinCancelled = false;
      devin = { ...devin, auth: "signing-in" };
      await sleep(2600); // the browser is open, the user signs in
      if (devinCancelled) return { ...devin };
      devin = { ...DEVIN_ACCOUNT, enabled: true };
      return { ...devin };
    },
    async cancelLogin() {
      devinCancelled = true;
      devin = { ...devin, auth: "signed-out" };
      return { ...devin };
    },
    async logout() {
      await sleep(300);
      devin = { ...devin, auth: "signed-out", email: undefined, plan: undefined };
      return { ...devin };
    },
    async setEnabled(on) {
      devin = { ...devin, enabled: on };
      return { ...devin };
    },
  },
  chatgpt: {
    account: async () => ({ ...chatgpt }),
    async login() {
      const attempt = ++chatgptAttempt;
      chatgptCancelled = false;
      chatgpt = { ...chatgpt, auth: "signing-in", signingIn: true };
      await sleep(2600); // the browser is open, the user signs in and agrees
      // (an attempt that was cancelled, or replaced by a newer one while it slept, changes nothing)
      if (chatgptCancelled || attempt !== chatgptAttempt) return { ...chatgpt };
      chatgpt = { ...CHATGPT_ACCOUNT, welcomed: false };
      return { ...chatgpt };
    },
    async cancelLogin() {
      chatgptCancelled = true;
      chatgpt = { ...chatgpt, auth: "signed-out", signingIn: false };
      return { ...chatgpt };
    },
    async logout() {
      await sleep(300);
      chatgpt = { ...chatgpt, auth: "signed-out", email: "", planEnabled: false, accounts: [], clientId: undefined };
      return { ...chatgpt };
    },
    async use() {
      return { ...chatgpt };
    },
    async setEnabled(on) {
      chatgpt = { ...chatgpt, enabled: on };
      return { ...chatgpt };
    },
    async welcomed() {
      chatgpt = { ...chatgpt, welcomed: true };
      return { ...chatgpt };
    },
  },
  providers: {
    async save(d) {
      await sleep(250);
      return { ...d, keyHint: d.keyMode === "none" ? "" : d.keyHint, status: undefined } satisfies ApiProvider;
    },
    async remove() {
      await sleep(150);
    },
    async test(d) {
      await sleep(900);
      return fakeServer(d);
    },
    async listModels(d) {
      await sleep(700);
      // (the demo server gives the figures for some models, the way OpenRouter does; OpenAI's list would give only ids)
      const figures: Record<string, { contextWindow: number; maxOutput: number }> = {
        "openai/gpt-oss-120b": { contextWindow: 131_072, maxOutput: 16_384 },
        "qwen/qwen3-coder": { contextWindow: 262_144, maxOutput: 32_768 },
      };
      return (fakeServer(d).models ?? []).map((id) => ({ id, ...figures[id] }));
    },
    async envStatus(names) {
      return Object.fromEntries(names.map((n) => [n, envState(n)]));
    },
  },
  memory: mockMemory,
  importer: mockImporter,
  window: {
    async minimize() {},
    async toggleMaximize() {
      maximized = !maximized;
      return maximized;
    },
    async close() {},
    async startMove() {
      // lets the checks see that a press on the title bar reached the bridge
      const w = window as unknown as { __moves?: number };
      w.__moves = (w.__moves ?? 0) + 1;
    },
    async startResize() {},
    async isMaximized() {
      return maximized;
    },
  },
};
