import { parseAsync, renderDocument, type HElement } from "docx-preview";

const HTML = "http://www.w3.org/1999/xhtml";
const SVG = "http://www.w3.org/2000/svg";
const MATH = "http://www.w3.org/1998/Math/MathML";
const htmlTags = new Set("a article b br col colgroup del div em footer h1 h2 h3 h4 h5 h6 header i img ins li ol p section source span strong style sub sup table tbody td th thead tr u ul wbr".split(" "));
const svgTags = new Set("svg ellipse g image line rect foreignobject".split(" "));
const mathTags = new Set("math mi mn mo mrow mtext msqrt mroot mfrac msup msub msubsup munderover munder mover mtable mtr mtd".split(" "));
const allowedTags = new Map([[HTML, htmlTags], [SVG, svgTags], [MATH, mathTags]]);
const svgGeometry = new Set("width height x y x1 y1 x2 y2 rx ry cx cy".split(" "));
const plain = new Set(["class", "id", "alt", "title", "colspan", "rowspan", "datetime"]);
const dataImage = /^data:image\/(?:png|jpeg|gif|bmp|webp|svg\+xml|x-icon);base64,[\w+/=]+$/i;
const dataFont = /^data:(?:font\/[\w.+-]+|application\/(?:octet-stream|x-font-ttf|vnd\.openxmlformats-officedocument\.obfuscatedfont));base64,[\w+/=]+$/i;

export function wordHref(href: string): string | null {
  const value = href.trim();
  if (/[\u0000-\u001f\u007f]/.test(value)) return null;
  if (value.startsWith("#")) return value;
  if (!/^(?:https?:\/\/|mailto:)/i.test(value)) return null;
  try { return new URL(value).href; } catch { return null; }
}

// CSS also has resource-loading syntax. Permit only known layout functions and explicit, local base64 resources.
export function safeWordCss(value: string): boolean {
  if (/[\\\u0000-\u001f\u007f]|\/\*/.test(value)) return false;
  const rest = value.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^\s)]+))\s*\)/gi, (all, double, single, bare) => {
    const url: string = double ?? single ?? bare;
    return dataImage.test(url) || dataFont.test(url) ? "" : all;
  });
  return [...rest.matchAll(/([\w-]+)\(/g)].every(([, name]) => /^(?:var|rgb|rgba|hsl|hsla|calc|min|max|clamp|counter|counters|rotate|scale|translate|matrix|rect|inset)$/i.test(name));
}

function cleanDeclarations(style: CSSStyleDeclaration) {
  for (const name of Array.from(style)) {
    if (!safeWordCss(style.getPropertyValue(name))) style.removeProperty(name);
  }
}

function cleanStylesheet(text: string): string {
  // Constructed stylesheets never load @import and no rule is applied until after this pass.
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(text);
  const rules = [];
  for (const rule of Array.from(sheet.cssRules)) {
    if (rule instanceof CSSStyleRule && !/\\|:host/i.test(rule.selectorText)) {
      cleanDeclarations(rule.style);
      // Rebuild leaves out nested rules, including nested selectors and at-rules.
      rules.push(`${rule.selectorText} { ${rule.style.cssText} }`);
    } else if (rule instanceof CSSFontFaceRule) {
      cleanDeclarations(rule.style);
      rules.push(rule.cssText);
    }
  }
  return rules.join("\n");
}

export function cleanWord(root: ParentNode) {
  for (const el of Array.from(root.querySelectorAll("*"))) {
    const tag = el.localName.toLowerCase();
    const ns = el.namespaceURI ?? "";
    if (!allowedTags.get(ns)?.has(tag)) { el.remove(); continue; }
    if (tag === "style") el.textContent = cleanStylesheet(el.textContent ?? "");
    const media = tag === "img" || tag === "source" || tag === "image";
    const mediaAttr = tag === "image" ? "href" : "src";
    if (media && !dataImage.test(el.getAttribute(mediaAttr) ?? "")) {
      // The library may still size a VML drawing on the next animation frame.
      if (tag === "image" && el.parentElement?.localName === "svg") el.parentElement.remove();
      else el.remove();
      continue;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (attr.namespaceURI) { el.removeAttributeNode(attr); continue; }
      if (tag === "a" && name === "href") {
        const href = wordHref(attr.value);
        if (href !== null) el.setAttribute("href", href);
        else el.removeAttributeNode(attr);
      } else if (name === "style" && (el instanceof HTMLElement || el instanceof SVGElement || el instanceof MathMLElement)) {
        cleanDeclarations(el.style);
      } else if (!(plain.has(name) || (media && name === mediaAttr) || (ns === SVG && (svgGeometry.has(name) || (["stroke", "fill", "stroke-width"].includes(name) && safeWordCss(attr.value)))))) {
        el.removeAttributeNode(attr);
      }
    }
  }
}

/** The renderer creates nodes in an inert document, so even setting an img's src cannot start a request before cleanup. */
export async function readWord(bytes: Uint8Array): Promise<DocumentFragment> {
  const inert = document.implementation.createHTMLDocument("");
  const h = (value: HElement | Node | string): Node => {
    if (value instanceof Node) return value;
    if (typeof value === "string") return inert.createTextNode(value);
    const { ns = HTML, tagName, className, style, children = [] } = value;
    if (tagName === "#comment") return inert.createComment("");
    if (tagName === "#fragment") {
      const fragment = inert.createDocumentFragment();
      fragment.append(...children.map(h));
      return fragment;
    }
    const el = allowedTags.get(ns)?.has(tagName.toLowerCase()) ? inert.createElementNS(ns, tagName) : inert.createElement("span");
    if (className) el.setAttribute("class", className);
    if (typeof style === "string") el.setAttribute("style", style);
    else if (style && (el instanceof HTMLElement || el instanceof SVGElement || el instanceof MathMLElement)) Object.assign(el.style, style);
    for (const key of ["id", "href", "colSpan", "rowSpan", "dateTime"]) {
      if (value[key] !== undefined) el.setAttribute(key.toLowerCase(), String(value[key]));
    }
    el.append(...children.map(h));
    return el;
  };
  const options = { className: "fp-docx", useBase64URL: true, renderAltChunks: false, renderComments: false, experimental: false, h };
  const parsed = await parseAsync(bytes, options);
  const fragment = inert.createDocumentFragment();
  fragment.append(...await renderDocument(parsed, options));
  cleanWord(fragment);
  return fragment;
}
