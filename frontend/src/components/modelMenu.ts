import type { ApiProvider, ChatGPTAccount, DevinAccount, Engine, ModelChoice } from "../types";
import { engineLabel, engineOfSource } from "../lib/models.ts"; // (with the extension: scripts/unit.mjs runs this file in Node)
import type { MenuItem } from "./ui";

export interface ModelMenuInput {
  models: ModelChoice[];
  providers: ApiProvider[];
  devin: DevinAccount;
  chatgpt: ChatGPTAccount;
  current: string;
  /** model ids the user chose to leave out (settings → 账号与模型). The model in use (`current`) is offered anyway. */
  hidden?: string[];
  /** The engine of the open session. A model of the other engine changes the conversation's engine: the earlier text
   *  is handed over, which cannot happen while a turn is running. */
  engine?: Engine;
  running?: boolean;
  /** the session has no messages yet: changing engine loses nothing */
  fresh?: boolean;
  onPick: (id: string) => void;
  /** open the settings page where accounts and providers are managed */
  onManage: (providerEdit?: string | null) => void;
  /** start "Continue with ChatGPT" (the browser opens); without it the item opens the settings page instead */
  onChatGPTLogin?: (forceConsent: boolean) => void;
}

const host = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");

/** The models of one maker under a small heading each, in the order the engine lists them. No headings when no model
 *  says who made it. */
function byGroup(models: ModelChoice[]): [string | null, ModelChoice[]][] {
  if (!models.some((m) => m.group)) return [[null, models]];
  const groups = new Map<string, ModelChoice[]>();
  for (const m of models) {
    const name = m.group ?? "其他";
    groups.set(name, [...(groups.get(name) ?? []), m]);
  }
  return [...groups];
}

/** The model menu: ChatGPT Plan's models and Devin's (or the way to sign in), then each API provider's, then the way to add one.
 *  Models an engine lists as disabled are left out, and so are the ones the user hid (unless one is the model in use). */
export function buildModelMenu(i: ModelMenuInput): MenuItem[] {
  const items: MenuItem[] = [];
  const hidden = new Set(i.hidden ?? []);
  const available = i.models.filter((m) => !m.disabled);
  const usable = available.filter((m) => !hidden.has(m.id) || m.id === i.current);
  /** A group whose models are all hidden still says why it is empty, and where to change that. */
  const allHidden = (source: string) => !usable.some((m) => m.source === source) && available.some((m) => m.source === source);
  const noneShown = (source: string): MenuItem => ({
    id: `none-${source}`,
    label: "没有显示的模型",
    description: "在设置「账号与模型」里选择要显示的",
    disabled: true,
  });
  const foreign = (m: ModelChoice) => i.engine !== undefined && engineOfSource(m.source) !== i.engine;
  const crossing = (m: ModelChoice) => {
    const to = engineLabel(m.source);
    if (i.running) return `本轮结束后才能换到 ${to}`;
    return i.fresh ? `换到 ${to}（这个会话还是空的）` : `换到 ${to} 接着做，之前的文字会交接`;
  };
  const modelItem = (m: ModelChoice): MenuItem => ({
    id: m.id,
    label: m.label,
    description: foreign(m) ? crossing(m) : hidden.has(m.id) ? "已在设置里隐藏，正在使用所以还显示" : m.note,
    checked: m.id === i.current,
    disabled: foreign(m) && !!i.running,
    onSelect: () => i.onPick(m.id),
  });

  if (i.chatgpt.available && i.chatgpt.enabled) {
    const ready = i.chatgpt.auth === "signed-in" && i.chatgpt.planEnabled;
    const waiting = i.chatgpt.auth === "signing-in";
    const note = ready ? i.chatgpt.email : waiting ? "在浏览器里登录…" : i.chatgpt.auth === "signed-in" ? "没有授权使用套餐" : "未登录";
    items.push({ id: "h-chatgpt", heading: true, label: "CHATGPT PLAN", description: note });
    if (ready) {
      items.push(...usable.filter((m) => m.source === "chatgpt").map(modelItem));
      if (allHidden("chatgpt")) items.push(noneShown("chatgpt"));
    } else if (!waiting) {
      const again = i.chatgpt.auth === "signed-in"; // signed in without the permission to use the plan: ask for it again
      items.push({
        id: "chatgpt-login",
        label: again ? "允许使用 ChatGPT 套餐…" : "Continue with ChatGPT",
        description: again ? "已登录，但还没有同意让 WittyCode 使用你的套餐" : "用你的 ChatGPT 套餐，不需要 API Key",
        onSelect: () => (i.onChatGPTLogin ? i.onChatGPTLogin(again) : i.onManage(null)),
      });
    }
  }

  if (i.devin.enabled) {
    const checking = i.devin.auth === "checking";
    const ready = i.devin.auth === "signed-in";
    const note = checking ? "正在检测…" : ready ? i.devin.plan : "未登录";
    items.push({ id: "h-devin", heading: true, label: "DEVIN", description: note });
    if (ready) {
      // a model menu of fifty needs the makers as small headings, like a long model list does
      for (const [group, list] of byGroup(usable.filter((m) => m.source === "devin"))) {
        if (group) items.push({ id: `g-devin-${group}`, heading: true, sub: true, label: group });
        items.push(...list.map(modelItem));
      }
      if (allHidden("devin")) items.push(noneShown("devin"));
    } else if (!checking)
      items.push({
        id: "devin-login",
        label: i.devin.cli === "missing" ? "没有找到 devin 命令行…" : "登录 Devin…",
        description: i.devin.cli === "missing" ? "先安装 Devin 桌面版或 Devin CLI，再登录" : "用 Devin 账号登录，不需要 API Key",
        onSelect: () => i.onManage(null),
      });
  }

  const listed = new Set(i.providers.map((p) => `api:${p.id}`));
  for (const p of i.providers) {
    const source = `api:${p.id}`;
    const own = usable.filter((m) => m.source === source);
    // a provider without any model has nothing to show; one whose models are all hidden still says so, like the other sources
    if (own.length === 0 && !allHidden(source)) continue;
    items.push({ id: `h-${p.id}`, heading: true, label: `API · ${p.name.toUpperCase()}`, description: host(p.baseUrl) });
    items.push(...own.map(modelItem));
    if (allHidden(source)) items.push(noneShown(source));
  }
  // Model entries that none of the providers owns (added by hand to the models file): Python names their source "api:other".
  const orphans = new Map<string, ModelChoice[]>();
  for (const m of available) if (m.source.startsWith("api:") && !listed.has(m.source) && !orphans.has(m.source)) orphans.set(m.source, []);
  for (const m of usable) if (orphans.has(m.source)) orphans.get(m.source)!.push(m);
  for (const [source, own] of orphans) {
    const name = source === "api:other" ? "其他" : source.slice(4).toUpperCase();
    items.push({ id: `h-${source}`, heading: true, label: `API · ${name}` });
    items.push(...own.map(modelItem));
    if (own.length === 0) items.push(noneShown(source));
  }

  items.push({ id: "sep-manage", separator: true, pinned: true });
  items.push({ id: "add-provider", label: "添加 API Provider…", pinned: true, onSelect: () => i.onManage("new") });
  items.push({ id: "manage", label: "管理账号与模型…", pinned: true, onSelect: () => i.onManage(null) });
  return items;
}
