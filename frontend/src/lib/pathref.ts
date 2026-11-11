// Which pieces of the model's text name a file the user may want to open: `docs/x.md`, `src/a.py:42`, `D:\work\b.txt:7-9`.
// A guess from the look of the text alone (the page cannot look at the disk): a path that turns out not to be there is
// answered with a sentence when it is clicked. The look must be strict enough that code in backticks (`self.state`,
// `os.path`, `read/write`, `v1.2`) stays plain code.

/** Extensions that make a bare name (no folder in front) a file: `README.md`, `app_core.py`. */
const BARE = new Set(
  (
    "md markdown mdx txt py pyw ts tsx js jsx mjs cjs json jsonl yaml yml toml css scss html htm log ini cfg conf csv tsv xml sql rs go " +
    "java kt c h cpp hpp cs cu cuh sh ps1 bat cmd vue svelte lock env gitignore pdf png jpg jpeg gif svg webp docx xlsx pptx zip patch diff ipynb rst tex proto"
  ).split(" "),
);

/** Files that are only an extension: `.gitignore`, `.env`. */
const DOTFILES = new Set("gitignore gitattributes gitmodules env editorconfig npmrc prettierrc eslintrc dockerignore".split(" "));

const NAME = /^[\p{L}\p{N}_.\-@+~ ()[\]]+$/u;
const SUFFIX = /:(\d+)(?:-(\d+))?(?::\d+)?$/;

export interface Ref {
  path: string;
  line: number | null;
  end: number | null;
}

/** The path and the lines of a reference, or null when the text does not look like a path to a file. */
export function parsePathRef(text: string): Ref | null {
  const s = text.trim();
  if (s.length < 3 || s.length > 260 || /[\n\r\t<>|?*"`{}=;,$!#%^&]/.test(s) || s.includes("://")) return null;
  const found = SUFFIX.exec(s);
  const path = found && found.index > 1 ? s.slice(0, found.index) : s;
  const drive = /^[A-Za-z]:[\\/]/.test(path);
  const rest = drive ? path.slice(2) : path;
  if (rest.includes(":") || /^[-@+]/.test(path)) return null;
  const absolute = drive || path.startsWith("/") || path.startsWith("\\\\") || path.startsWith("~");
  if (/\s/.test(path) && !absolute) return null;
  const segments = path.split(/[\\/]/);
  const name = segments[segments.length - 1] ?? "";
  const ext = /\.([A-Za-z0-9]{1,10})$/.exec(name)?.[1]?.toLowerCase();
  if (!ext || !NAME.test(name)) return null;
  if (name === `.${ext}`) {
    if (!DOTFILES.has(ext)) return null; // (`.py` in a sentence names a kind of file, not a file)
  } else if (segments.length === 1 && !BARE.has(ext)) return null;
  const [line, end] = found && found.index > 1 ? [Number(found[1]), found[2] ? Number(found[2]) : null] : [null, null];
  return { path, line, end };
}

export const looksLikePath = (text: string): boolean => parsePathRef(text) !== null;

/** A link target of the model's text that is a local file rather than an address: a relative path or a `file:` address. */
export function localTarget(href: string | undefined): string | null {
  if (!href) return null;
  if (/^(https?|mailto|tel|ftp):/i.test(href) || href.startsWith("#")) return null;
  let text = href;
  if (/^file:/i.test(text)) {
    try {
      text = decodeURIComponent(new URL(text).pathname).replace(/^\/([A-Za-z]:)/, "$1");
    } catch {
      return null;
    }
  } else {
    try {
      text = decodeURIComponent(text);
    } catch {
      /* not encoded: as it is */
    }
  }
  text = text.replace(/^\/([A-Za-z]:[\\/])/, "$1"); // (`/D:/work/a.md`, the way a path is often written in a link)
  return parsePathRef(text) ? text : null;
}
