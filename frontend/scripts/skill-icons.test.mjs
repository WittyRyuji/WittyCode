import assert from "node:assert/strict";
import test from "node:test";
import { skillIconKind } from "../src/lib/skillIcons.ts";

test("skill store icons reflect purpose and always provide a local fallback", () => {
  for (const [name, kind] of [
    ["pdf", "document"], ["openai-docs", "document"], ["PPTX", "document"],
    ["spreadsheet", "table"], ["jupyter-notebook", "table"],
    ["figma-implement-design", "design"], ["frontend-design", "design"],
    ["screenshot", "image"], ["imagegen", "image"],
    ["playwright-interactive", "browser"], ["webapp-testing", "browser"],
    ["transcribe", "audio"], ["speech", "audio"], ["gh-fix-ci", "code"],
    ["security-best-practices", "code"], ["skill-creator", "skill"], ["unknown-skill", "skill"], ["", "skill"],
  ]) assert.equal(skillIconKind(name), kind, name);
});
