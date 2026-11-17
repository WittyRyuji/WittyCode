import type { ModelChoice } from "../types";

/** Respect the same visibility setting as the conversation's model menu, including for ACP experts. */
export function fusionChoices(models: ModelChoice[], hidden: string[], includeDevin: boolean): ModelChoice[] {
  const omitted = new Set(hidden);
  return models.filter((model) => !omitted.has(model.id) && (model.source === "chatgpt" || model.source.startsWith("api:") || (includeDevin && model.source === "devin")));
}

/** Fusion's ACP adapter runs tools through the same permission bridge as the own model runner. */
export function fusionDisabledReason(model: ModelChoice, _main = false): string {
  return model.disabledReason ?? "";
}

/** One effort override per model, even when it fills several Fusion roles. Hidden selections stay editable. */
export function fusionSelectedModels(main: string, workers: string[], light: string): { id: string; roles: string[] }[] {
  const selected = new Map<string, string[]>();
  const entries: [string, string][] = [[main, "主模型"], ...workers.map((id): [string, string] => [id, "工作模型"]), [light, "轻量模型"]];
  for (const [id, role] of entries) {
    if (!id) continue;
    const roles = selected.get(id) ?? [];
    if (!roles.includes(role)) roles.push(role);
    selected.set(id, roles);
  }
  return [...selected].map(([id, roles]) => ({ id, roles }));
}
