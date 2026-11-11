// The ChatGPT Plan engine's words on the page. OpenAI's UI guidelines for apps that use a ChatGPT plan ask for these: the
// sign-in button says "Continue with ChatGPT", an app that is using the plan says "Using ChatGPT plan" near the composer
// or the model selector with a "Manage usage" link to the user's ChatGPT usage settings (the page writes the plan with a
// capital P, as the user asked).

import type { ChatGPTAccount, CodexAccount, LimitWindow } from "../types";

/** Where the user sees what apps used of the plan and sets each app's limit. */
export const CHATGPT_USAGE_URL = "https://chatgpt.com/settings/usage";

export type Tone = "ok" | "warn" | "off" | "busy";

/** The state line of the account card, and its tone. */
export function chatgptState(a: ChatGPTAccount): { text: string; tone: Tone } {
  if (!a.available) return { text: "这个版本里没有", tone: "off" };
  if (a.auth === "signing-in") return { text: "正在等你在浏览器里完成登录…", tone: "busy" };
  if (a.auth === "signed-in") {
    if (!a.planEnabled) return { text: `已登录 · ${a.email || "ChatGPT"} · 还没有允许使用套餐`, tone: "warn" };
    return { text: ["已登录", a.email, "使用你的 ChatGPT 套餐"].filter(Boolean).join(" · "), tone: "ok" };
  }
  return { text: "未登录", tone: "off" };
}

/** The account a conversation is paid by is usable: signed in and the plan allowed. */
export const chatgptReady = (a: ChatGPTAccount) => a.available && a.auth === "signed-in" && a.planEnabled;

/** How much of the plan is used. OpenAI gives an app signed in this way no way to read it (the usage endpoint refuses its
 *  token and the responses carry no limit headers), so the figures come from a Codex login of the same ChatGPT account, whose
 *  app-server reads them; without one they are unknown, and the reason is said. */
export type PlanUsage = { known: true; percent: number; plan: string; windows: LimitWindow[] } | { known: false; why: string };

export function planUsage(chatgpt: ChatGPTAccount, codex: CodexAccount): PlanUsage {
  if (codex.cli === "missing") return { known: false, why: "没有安装 Codex。" };
  if (codex.auth === "checking") return { known: false, why: "正在检查 Codex 的登录…" };
  if (codex.auth !== "chatgpt") return { known: false, why: "Codex 没有用 ChatGPT 账号登录。" };
  if (chatgpt.email && codex.email && chatgpt.email.toLowerCase() !== codex.email.toLowerCase()) return { known: false, why: `Codex 登录的是另一个账号（${codex.email}）。` };
  if (codex.limits.length === 0) return { known: false, why: "Codex 没有返回额度。" };
  return { known: true, percent: Math.max(...codex.limits.map((l) => l.usedPercent)), plan: codex.plan ?? "", windows: codex.limits };
}

/** The ring's colour class: the same thresholds as the Limit figures of the status bar. */
export const usageLevel = (percent: number) => (percent >= 95 ? "crit" : percent >= 80 ? "warn" : "");
