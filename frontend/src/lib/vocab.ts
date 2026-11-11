// The words the page uses for settings values: reasoning levels, permission levels, modes, API formats and the provider
// presets. Shared by the design mock and the live app; the values are the ones Python and Droid use.

import type { ApiKind, ApiProvider, Effort, Engine, ModelChoice } from "../types";

/** Every level Droid knows (droid_sdk ReasoningEffort), with the wording of the menu. A model offers some of them. */
export const REASONING: { value: Effort; label: string; hint: string }[] = [
  { value: "none", label: "无", hint: "不用推理" },
  { value: "dynamic", label: "动态", hint: "由模型自己决定思考多少" },
  { value: "off", label: "Off", hint: "不思考，直接回答" },
  { value: "minimal", label: "Minimal", hint: "几乎不思考" },
  { value: "low", label: "Low", hint: "少量思考，最快" },
  { value: "medium", label: "Medium", hint: "速度和质量折中" },
  { value: "high", label: "High", hint: "较多思考，适合复杂任务" },
  { value: "xhigh", label: "XHigh", hint: "更多思考，更慢更贵" },
  { value: "max", label: "Max", hint: "最大思考量，最慢最贵" },
  { value: "ultra", label: "Ultra", hint: "比 Max 还多的思考量（Codex 模型）" },
];

/** A level the menu has no wording for (Codex may add its own) keeps its name, with a capital like the others. */
const capitalised = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

/** The menu's word for a level. */
export const effortLabel = (value: string) => REASONING.find((r) => r.value === value)?.label ?? capitalised(value);

/** The levels a model offers, in the model's own order (unknown model: all of them). */
export function reasoningChoices(model: ModelChoice | undefined): { value: string; label: string; hint?: string }[] {
  if (!model) return REASONING;
  return model.efforts.map((value) => REASONING.find((r) => r.value === value) ?? { value, label: capitalised(value) });
}

/** The session's permission level: how much the agent may do without asking. Each engine has its own scale: Droid's
 *  "autonomy", for Codex the sandbox together with when it asks (the values of Python's CODEX_PERMISSIONS), for Devin its
 *  modes (DEVIN_PERMISSIONS). Least to most: the first entry is the engine's starting level, except where `START_LEVELS`
 *  says otherwise. */
export const PERMISSIONS: Record<Engine, { value: string; label: string; hint: string }[]> = {
  droid: [
    { value: "off", label: "OFF", hint: "每个有风险的操作都先问我" },
    { value: "low", label: "Low", hint: "自动放行只读和低风险操作" },
    { value: "medium", label: "Medium", hint: "自动放行大多数文件和命令操作" },
    { value: "high", label: "High", hint: "几乎不询问，请只在可信的项目里使用" },
  ],
  codex: [
    { value: "readonly", label: "只读", hint: "只能读文件；改文件和跑命令都先问我" },
    { value: "workspace", label: "工作区", hint: "可以在项目文件夹里改文件、跑命令；要越出去先问我" },
    { value: "full", label: "完全访问", hint: "没有沙箱，也不再询问，请只在可信的项目里使用" },
  ],
  devin: [
    { value: "ask", label: "问答", hint: "只回答问题，不改代码" },
    { value: "plan", label: "计划", hint: "先出方案，批准后再动手" },
    { value: "code", label: "写代码", hint: "可以改文件；运行有风险的命令前先问我" },
    { value: "smart", label: "智能", hint: "模型判断为安全的操作自动放行，其余先问我" },
    { value: "full", label: "完全访问", hint: "所有操作自动放行（组织策略要求审批的除外），请只在可信的项目里使用" },
  ],
  chatgpt: [
    { value: "ask", label: "询问", hint: "只读操作直接做；改文件和运行命令都先问我" },
    { value: "code", label: "写代码", hint: "项目里改文件直接做；运行命令（简单的只读命令除外）和越出项目的操作先问我" },
    { value: "full", label: "完全访问", hint: "什么都不问，也没有沙箱，请只在可信的项目里使用" },
  ],
};
export const permissionsFor = (engine: Engine) => PERMISSIONS[engine];
/** Where a new session of an engine starts when it is not the first of the scale (Devin's own default is "Code"). */
const START_LEVELS: Partial<Record<Engine, string>> = { devin: "code", chatgpt: "code" };
export const startingLevel = (engine: Engine) => START_LEVELS[engine] ?? PERMISSIONS[engine][0]!.value;

/** `spec` is Droid's own value for what the page calls Plan. */
export const MODES = [
  { value: "auto", label: "Auto", hint: "直接动手完成任务" },
  { value: "spec", label: "Plan", hint: "先出方案，批准后再动手" },
];

/** The quick starts of the provider editor: format, address and what to ask for. */
export interface Preset {
  id: string;
  name: string;
  kind: ApiKind;
  baseUrl: string;
  keyMode: ApiProvider["keyMode"];
  envName?: string;
  /** the first model's id when the preset lists no `models` */
  example: string;
  local?: boolean;
  /** send the key as `Authorization: Bearer` (Anthropic format only) */
  bearer?: boolean;
  /** what the editor starts with, in place of `example` */
  models?: { id: string; displayName?: string; contextWindow?: number; images: boolean }[];
}

// The two Chinese services speak the Anthropic Messages format as well as OpenAI's; that one is chosen because both
// vendors' own Claude Code guides use it, and thinking blocks and tool calls travel in it the way Droid sends them.
// MiMo's Token Plan keys (`tp-…`, team plans `ttp-…`) work only with the Token Plan's own addresses, one per region,
// and its guide sends the key as a bearer token.
const MIMO_MODELS: NonNullable<Preset["models"]> = [
  { id: "mimo-v2.6-pro", displayName: "MiMo V2.6 Pro", images: true },
  { id: "mimo-v2.6-flash", displayName: "MiMo V2.6 Flash", images: true },
];
const mimo = (id: string, region: string, host: string): Preset => ({
  id,
  name: `小米 MiMo Token Plan（${region}）`,
  kind: "anthropic",
  baseUrl: `https://${host}.xiaomimimo.com/anthropic`,
  keyMode: "env",
  envName: "MIMO_API_KEY",
  example: "mimo-v2.6-pro",
  bearer: true,
  models: MIMO_MODELS,
});

export const PRESETS: Preset[] = [
  mimo("mimo-cn", "中国", "token-plan-cn"),
  mimo("mimo-sgp", "新加坡", "token-plan-sgp"),
  mimo("mimo-ams", "欧洲", "token-plan-ams"),
  {
    id: "deepseek",
    name: "DeepSeek",
    kind: "anthropic",
    baseUrl: "https://api.deepseek.com/anthropic",
    keyMode: "env",
    envName: "DEEPSEEK_API_KEY",
    example: "deepseek-v4-pro",
    models: [
      // DeepSeek's own table: Pro does not read pictures, Flash does; both have a 1M window
      { id: "deepseek-v4-pro", displayName: "DeepSeek V4 Pro", contextWindow: 1_000_000, images: false },
      { id: "deepseek-flash", displayName: "DeepSeek Flash", contextWindow: 1_000_000, images: true },
    ],
  },
  { id: "openai", name: "OpenAI 官方", kind: "openai", baseUrl: "https://api.openai.com/v1", keyMode: "env", envName: "OPENAI_API_KEY", example: "gpt-5.3-codex" },
  { id: "anthropic", name: "Anthropic 官方", kind: "anthropic", baseUrl: "https://api.anthropic.com", keyMode: "env", envName: "ANTHROPIC_API_KEY", example: "claude-sonnet-4-5-20250929" },
  { id: "openrouter", name: "OpenRouter", kind: "generic-chat-completion-api", baseUrl: "https://openrouter.ai/api/v1", keyMode: "env", envName: "OPENROUTER_API_KEY", example: "openai/gpt-oss-120b" },
  { id: "fireworks", name: "Fireworks AI", kind: "generic-chat-completion-api", baseUrl: "https://api.fireworks.ai/inference/v1", keyMode: "env", envName: "FIREWORKS_API_KEY", example: "accounts/fireworks/models/deepseek-v3p1-terminus" },
  { id: "groq", name: "Groq", kind: "generic-chat-completion-api", baseUrl: "https://api.groq.com/openai/v1", keyMode: "env", envName: "GROQ_API_KEY", example: "moonshotai/kimi-k2-instruct-0905" },
  { id: "gemini", name: "Google Gemini", kind: "generic-chat-completion-api", baseUrl: "https://generativelanguage.googleapis.com/v1beta/", keyMode: "env", envName: "GEMINI_API_KEY", example: "gemini-2.5-pro" },
  { id: "ollama", name: "Ollama（本机）", kind: "generic-chat-completion-api", baseUrl: "http://localhost:11434/v1", keyMode: "none", example: "qwen3:30b", local: true },
  { id: "lmstudio", name: "LM Studio（本机）", kind: "generic-chat-completion-api", baseUrl: "http://localhost:1234/v1", keyMode: "none", example: "你加载的模型", local: true },
  { id: "bedrock", name: "AWS Bedrock", kind: "bedrock-converse", baseUrl: "", keyMode: "none", example: "anthropic.claude-sonnet-4-5-20250929-v1:0" },
  { id: "custom", name: "其他（自己填）", kind: "generic-chat-completion-api", baseUrl: "", keyMode: "env", example: "" },
];

export const KIND_INFO: Record<ApiKind, { label: string; hint: string }> = {
  anthropic: { label: "Anthropic Messages", hint: "Anthropic 官方 API 和兼容 Messages 接口的代理" },
  openai: { label: "OpenAI Responses", hint: "OpenAI 官方 API 和兼容 Responses 接口的代理；GPT-5 系列必须用它" },
  "generic-chat-completion-api": { label: "OpenAI Chat Completions", hint: "OpenRouter、Fireworks、Groq、Ollama、vLLM 等绝大多数第三方和本地服务" },
  "bedrock-converse": { label: "AWS Bedrock Converse", hint: "AWS Bedrock 上的模型，用 AWS 凭据，不需要 Base URL 和密钥" },
};

/** What the first message box and an empty conversation offer to start from. */
export const SUGGESTIONS = ["解释一下这个项目的结构", "找出最近改动里可能的 bug", "给最复杂的函数补测试"];
