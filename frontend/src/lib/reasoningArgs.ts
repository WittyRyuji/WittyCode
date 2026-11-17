import { API_EFFORTS } from "./providerDefaults.ts";

export function reasoningArgsProblem(text: string | undefined): string {
  if (!text?.trim()) return "";
  let value: unknown;
  try { value = JSON.parse(text); } catch { return "思考档位参数不是合法的 JSON。"; }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return "思考档位参数要是一个 JSON 对象。";
  for (const [effort, fragment] of Object.entries(value)) {
    if (!API_EFFORTS.some((level) => level === effort) || typeof fragment !== "object" || fragment === null || Array.isArray(fragment)) {
      return "思考档位参数的键只能是 off/minimal/low/medium/high/xhigh/max，值必须是 JSON 对象；片段内的 null 删除对应顶层字段。";
    }
  }
  const finite = (v: unknown): boolean => typeof v === "number" ? Number.isFinite(v) : v !== null && typeof v === "object" ? Object.values(v).every(finite) : true;
  if (!finite(value)) return "思考档位参数必须是合法 JSON，不能包含非有限数值。";
  return "";
}

export function hasAdvancedProviderOptions(provider: { headers: unknown[]; extraArgs: string; reasoningArgs?: string; reasoningArgsMixed?: boolean } | undefined): boolean {
  return !!provider && (provider.headers.length > 0 || !!provider.extraArgs || !!provider.reasoningArgs || !!provider.reasoningArgsMixed);
}
