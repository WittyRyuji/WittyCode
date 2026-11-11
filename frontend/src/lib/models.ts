import type { ApiProvider, Engine, ModelChoice, Session } from "../types";

/** The engine that runs a model of this source. An API provider's models are run by the application's own loop, the engine
 *  that also runs the ChatGPT plan's (Bedrock's too: it signs its own requests with the user's AWS credentials). */
export const engineOfSource = (source: string): Engine =>
  source === "codex" ? "codex" : source === "devin" ? "devin" : source === "chatgpt" || source.startsWith("api:") ? "chatgpt" : "droid";

const ENGINE_NAMES: Record<Engine, string> = { droid: "Droid", codex: "Codex", devin: "Devin", chatgpt: "ChatGPT Plan" };

/** What the page calls the place a model runs. An API provider's model is run by a droid process, but to the user it is
 *  "API" (the word on the model chip), not Factory's Droid. */
export const engineLabel = (source: string): string => (source.startsWith("api:") ? "API" : ENGINE_NAMES[engineOfSource(source)]);

export function findModel(models: ModelChoice[], id: string): ModelChoice | undefined {
  return models.find((m) => m.id === id);
}

/** The engine a session runs on: what the live app says, otherwise what its model implies. */
export function engineOfSession(models: ModelChoice[], s: Pick<Session, "settings" | "engine">): Engine {
  if (s.engine) return s.engine;
  const m = findModel(models, s.settings.model);
  return m ? engineOfSource(m.source) : "droid";
}

/** `Codex` / `Devin` / `ChatGPT Plan` / `OpenRouter` / "" (Factory's own models carry no label): what the sidebar and the model chip add. */
export function sourceLabel(source: string, providers: ApiProvider[]): string {
  if (source === "factory") return "";
  if (source === "codex") return "Codex";
  if (source === "devin") return "Devin";
  if (source === "chatgpt") return "ChatGPT Plan";
  return providers.find((p) => `api:${p.id}` === source)?.name.replace(/[（(].*$/, "") ?? "API";
}

/** `OPENROUTER_API_KEY` out of `${OPENROUTER_API_KEY}`; null when it is not a well-formed reference. */
export function envNameOf(hint: string): string | null {
  const m = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(hint.trim());
  return m ? m[1]! : null;
}

/** What the environment-variable field shows for a stored reference: the bare name (`MIMO_API_KEY` for `${MIMO_API_KEY}`).
 *  Only the wrapper is taken off, so a half-typed or malformed name is shown as typed. */
export function envVarText(hint: string): string {
  return hint.trim().replace(/^\$\{/, "").replace(/\}$/, "");
}

/** The reference to store for what was typed in that field: `${NAME}`; a pasted `${NAME}` is accepted too, and nothing
 *  typed stores nothing. Typing the name only is the point: the braces are easy to get wrong. */
export function envHintOf(typed: string): string {
  const name = envVarText(typed);
  return name ? `\${${name}}` : "";
}

/** The little tag on the model chip: where the model is billed, when that is not Factory. */
export function sourceTag(source: string): string {
  return source === "factory" ? "" : source === "codex" || source === "devin" || source === "chatgpt" ? "订阅" : "API";
}
