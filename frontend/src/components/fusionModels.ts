import type { ModelChoice } from "../types";

/** Respect the same visibility setting as the conversation's model menu, including for ACP experts. */
export function fusionChoices(models: ModelChoice[], hidden: string[]): ModelChoice[] {
  const omitted = new Set(hidden);
  return models.filter((model) => !omitted.has(model.id) && !!model.fusionBackend);
}

/** Fusion's ACP adapter runs tools through the same permission bridge as the own model runner. */
export function fusionDisabledReason(model: ModelChoice, _main = false): string {
  return model.disabledReason ?? "";
}
