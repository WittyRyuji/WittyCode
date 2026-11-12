import type { ApiKind, ApiModel, RemoteModel } from "../types";
import specifications from "../../../desktop/core/llm/model_specs.json" with { type: "json" };

export const API_EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const;

export const modelSpecification = (id: string): { contextWindow: number; source: string } | undefined => {
  const models = specifications.models as Record<string, { contextWindow: number; source: string }>;
  return Object.hasOwn(models, id.trim()) ? models[id.trim()] : undefined;
};

export const contextWindowOf = (model: ApiModel): number | undefined => model.contextWindow ?? modelSpecification(model.id)?.contextWindow;

export const contextProblem = (model: ApiModel): string => {
  const window = contextWindowOf(model);
  return window && Number.isSafeInteger(window) && window > 0 ? "" : `「${model.id}」的上下文窗口未知或不合法：请获取服务端数据，或填写服务商公布的上限。`;
};

/** A catalog fallback is not a user override: keep it empty in the draft so later table updates still apply. */
export const withRemoteFigures = (model: ApiModel, remote: RemoteModel | undefined): ApiModel => remote
  ? { ...model, contextWindow: model.contextWindow ?? (remote.contextSource === "catalog" ? undefined : remote.contextWindow), maxOutput: model.maxOutput ?? remote.maxOutput }
  : model;

/** Whether a model of this API format takes pictures when nobody said. The usual default (what is assumed for a
 *  `customModels` entry without `noImageSupport`, and what the editor's note promises): off for the generic chat
 *  completion format, on for Anthropic, OpenAI and Bedrock. */
export const imagesByDefault = (kind: ApiKind): boolean => kind !== "generic-chat-completion-api";

/** A model row's picture switch after the API format changed: a row still at the old format's default follows the new
 *  format's default, one the user set to something else keeps what they chose. */
export const imagesAfterKindChange = (images: boolean, from: ApiKind, to: ApiKind): boolean =>
  images === imagesByDefault(from) ? imagesByDefault(to) : images;
