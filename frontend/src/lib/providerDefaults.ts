import type { ApiKind } from "../types";

// Type imports only: scripts/unit.mjs runs this file in plain Node, which cannot resolve extensionless imports.

/** Whether a model of this API format takes pictures when nobody said. The usual default (what is assumed for a
 *  `customModels` entry without `noImageSupport`, and what the editor's note promises): off for the generic chat
 *  completion format, on for Anthropic, OpenAI and Bedrock. */
export const imagesByDefault = (kind: ApiKind): boolean => kind !== "generic-chat-completion-api";

/** A model row's picture switch after the API format changed: a row still at the old format's default follows the new
 *  format's default, one the user set to something else keeps what they chose. */
export const imagesAfterKindChange = (images: boolean, from: ApiKind, to: ApiKind): boolean =>
  images === imagesByDefault(from) ? imagesByDefault(to) : images;
