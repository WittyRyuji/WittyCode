// A cheap preflight for Markdown destinations, not an authority for reading files. Python independently
// checks the decoded path, the owning project's filesystem boundary, and the media signature.
const IMAGE = /\.(png|apng|jpe?g|jpe|jfif|gif|webp)$/i;
const VIDEO = /\.(mp4|m4v|mov|3gp|webm|mkv)$/i;
export const INLINE_IMAGE_DATA = /^data:image\/(?:png|apng|jpeg|gif|webp);base64,/;

/** An unsafe local video destination must not silently fall back to the less restricted file panel. */
export function localVideoLink(url: string | undefined): boolean {
  if (!url || /^(?:https?|mailto|tel|ftp):/i.test(url)) return false;
  try { return VIDEO.test(decodeURIComponent(url)); } catch { return false; }
}

export function inlineMediaTarget(url: string | undefined): { path: string; kind: "image" | "video" } | null {
  if (!url || url.length > 1536) return null;
  let path: string;
  try { path = decodeURIComponent(url); } catch { return null; }
  if (path.length > 512 || path !== path.trim() || /[\x00-\x1f<>:"|?*#%]/.test(path)) return null;
  const parts = path.replace(/\\/g, "/").replace(/^\.\//, "").split("/");
  if (parts.length > 32 || parts.some((part) => !part || part.startsWith("~") || /[ .]$/.test(part))) return null;
  if (IMAGE.test(path)) return { path, kind: "image" };
  if (VIDEO.test(path)) return { path, kind: "video" };
  return null;
}
