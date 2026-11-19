export type FusionRole = "main" | "worker" | "light";

/** Old per-model settings remain readable until that model's first role-specific edit. */
export function fusionEffort(values: Record<string, string>, role: FusionRole, model: string): string {
  return values[`${role}:${model}`] ?? values[model] ?? "";
}

export function withFusionEffort(values: Record<string, string>, role: FusionRole, model: string, effort: string): Record<string, string> {
  const next = { ...values };
  const legacy = next[model];
  if (legacy !== undefined) {
    for (const item of ["main", "worker", "light"] as const) {
      next[`${item}:${model}`] ??= legacy;
    }
    delete next[model];
  }
  if (effort) next[`${role}:${model}`] = effort;
  else delete next[`${role}:${model}`];
  return next;
}
