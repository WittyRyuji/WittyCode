/** Local, purpose-based icons, not unofficial copies of repository or vendor logos. */
export type SkillIconKind = "document" | "table" | "design" | "image" | "browser" | "audio" | "code" | "skill";

export function skillIconKind(name: string): SkillIconKind {
  const id = name.toLowerCase();
  if (/(^|[-_])(pdf|docx?|docs|pptx?|slides?|writing)([-_]|$)/.test(id)) return "document";
  if (/spreadsheet|xlsx|excel|notebook|data-analysis/.test(id)) return "table";
  if (/figma|design|brand|canvas/.test(id)) return "design";
  if (/image|screenshot|gif/.test(id)) return "image";
  if (/playwright|browser|webapp|web-game/.test(id)) return "browser";
  if (/speech|audio|transcrib/.test(id)) return "audio";
  if (/(^gh-)|github|code|test|security|deploy/.test(id)) return "code";
  return "skill";
}
