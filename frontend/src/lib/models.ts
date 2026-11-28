import type { ApiProvider, Engine, ModelChoice, Session } from "../types";

/** The engine that runs a model of this source. An API provider's models are run by the application's own loop, the engine
 *  that also runs the ChatGPT plan's (Bedrock's too: it signs its own requests with the user's AWS credentials). */
export const engineOfSource = (source: string): Engine => (source === "devin" ? "devin" : source === "factory" ? "factory" : "chatgpt");

export const GROK_SUBSCRIPTION_SOURCE = "api:grok-subscription";
const ENGINE_NAMES: Record<Engine, string> = { devin: "Devin", chatgpt: "ChatGPT Plan", factory: "Factory" };

/** What the page calls the place a model runs. An API provider's model runs in the own loop, but to the user it is
 *  "API" (the word on the model chip). */
export const engineLabel = (source: string): string => (source === GROK_SUBSCRIPTION_SOURCE ? "Grok 订阅" : source.startsWith("api:") ? "API" : ENGINE_NAMES[engineOfSource(source)]);

export function findModel(models: ModelChoice[], id: string): ModelChoice | undefined {
  return models.find((m) => m.id === id);
}

/** The engine a session runs on: what the live app says, otherwise what its model implies. */
export function engineOfSession(models: ModelChoice[], s: Pick<Session, "settings" | "engine">): Engine {
  if (s.engine) return s.engine;
  const m = findModel(models, s.settings.model);
  return m ? engineOfSource(m.source) : "chatgpt";
}

/** `Devin` / `ChatGPT Plan` / `OpenRouter` / "": what the sidebar and the model chip add. */
export function sourceLabel(source: string, providers: ApiProvider[]): string {
  if (source === "factory") return "Factory";
  if (source === "devin") return "Devin";
  if (source === "chatgpt") return "ChatGPT Plan";
  if (source === GROK_SUBSCRIPTION_SOURCE) return "Grok 订阅";
  return providers.find((p) => `api:${p.id}` === source)?.name.replace(/[（(].*$/, "") ?? "API";
}

/** `OPENROUTER_API_KEY` out of `${OPENROUTER_API_KEY}`; null when it is not a well-formed reference. */
export function envNameOf(hint: string): string | null {
  const m = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(hint.trim());
  return m ? m[1]! : null;
}

/** Names in a stored reference list. Reject the entire list if any of its slots is invalid. */
export function envNamesOf(hint: string): string[] {
  const names = hint.split(",").map(envNameOf);
  return names.every((name): name is string => name !== null) ? names : [];
}

/** Show bare names for a stored comma-separated list. */
export function envVarText(hint: string): string {
  return hint.split(",").map((part) => part.trim().replace(/^\$\{/, "").replace(/\}$/, "")).join(", ");
}

/** Store bare names as `${NAME}` references. Pasted references are accepted too. */
export function envHintOf(typed: string): string {
  return typed.split(",").map((part) => {
    const name = envNameOf(part) ?? part.trim();
    return name ? `\${${name}}` : "";
  }).join(",");
}

/** A saved literal key can be reused only at the same interface and Base URL (see provider_secrets.endpoint). */
export function sameProviderEndpoint(a: Pick<ApiProvider, "kind" | "baseUrl">, b: Pick<ApiProvider, "kind" | "baseUrl">): boolean {
  const clean = (url: string) => url.trim().replace(/\/+$/, "");
  return a.kind === b.kind && clean(a.baseUrl) === clean(b.baseUrl);
}

/** The little tag on the model chip: where the model is billed. */
export function sourceTag(source: string): string {
  return source === "devin" || source === "chatgpt" || source === "factory" || source === GROK_SUBSCRIPTION_SOURCE ? "订阅" : "API";
}
