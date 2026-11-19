import JSZip from "jszip";

const REL_NS = ["http://schemas.openxmlformats.org/officeDocument/2006/relationships", "http://purl.oclc.org/ooxml/officeDocument/relationships"];
const RASTER: Record<string, string> = { png: "png", jpg: "jpeg", jpeg: "jpeg", gif: "gif", bmp: "bmp" };

interface Paragraph { text: string; level: number; bullet: boolean }
type SlideBlock = { kind: "text"; paragraphs: Paragraph[] } | { kind: "table"; rows: string[][] } | { kind: "picture"; src: string; alt: string };
export interface Slide { title: string; blocks: SlideBlock[] }

const children = (node: Element | undefined, name?: string) => Array.from(node?.children ?? []).filter((el) => !name || el.localName === name);
const child = (node: Element | undefined, name: string) => children(node, name)[0];
const relId = (node: Element | undefined, name: string) => REL_NS.map((ns) => node?.getAttributeNS(ns, name)).find(Boolean) ?? "";

/** A package relationship is a ZIP path, not a URL. Never resolve against the application's base URL. */
function partName(source: string, target: string): string | null {
  let path;
  try { path = decodeURIComponent(target); } catch { return null; }
  if (!path || /[:?#\\\u0000-\u0020]/.test(path) || path.startsWith("//")) return null;
  const parts = path.startsWith("/") ? [] : source.split("/").slice(0, -1);
  for (const piece of path.split("/")) {
    if (!piece || piece === ".") continue;
    if (piece === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(piece);
  }
  return parts.join("/");
}

function paragraphs(body: Element | undefined): Paragraph[] {
  return children(body, "p").map((p) => {
    const props = child(p, "pPr");
    const text = children(p).map((run) => run.localName === "br" ? "\n" : child(run, "t")?.textContent ?? "").join("");
    const level = Math.max(0, Math.min(8, Number(props?.getAttribute("lvl")) || 0));
    const bullet = !child(props, "buNone") && (level > 0 || !!child(props, "buChar") || !!child(props, "buAutoNum"));
    return { text, level, bullet };
  }).filter((p) => p.text.trim());
}

/** Model only: all XML text stays plain text, and only embedded raster data can become an image. */
export async function readSlides(bytes: Uint8Array, parser: Pick<DOMParser, "parseFromString"> = new DOMParser()): Promise<Slide[]> {
  const zip = await JSZip.loadAsync(bytes);
  const readXml = async (path: string): Promise<Element | undefined> => {
    const text = await zip.file(path)?.async("string");
    if (!text || /<!DOCTYPE/i.test(text)) return undefined;
    const doc = parser.parseFromString(text, "application/xml");
    return doc.getElementsByTagName("parsererror").length ? undefined : doc.documentElement;
  };
  const relationships = async (source: string) => {
    const at = source.lastIndexOf("/") + 1;
    const root = await readXml(`${source.slice(0, at)}_rels/${source.slice(at)}.rels`);
    const targets = new Map<string, { path: string; type: string }>();
    for (const rel of children(root, "Relationship")) {
      if ((rel.getAttribute("TargetMode") ?? "Internal").toLowerCase() !== "internal") continue;
      const path = partName(source, rel.getAttribute("Target") ?? "");
      if (path) targets.set(rel.getAttribute("Id") ?? "", { path, type: (rel.getAttribute("Type") ?? "").split("/").at(-1) ?? "" });
    }
    return targets;
  };

  const main = "ppt/presentation.xml";
  const presentation = await readXml(main);
  const list = child(presentation, "sldIdLst");
  if (presentation?.localName !== "presentation") throw new Error("Missing presentation");
  const order = await relationships(main);
  const images = new Map<string, string>();
  const slides: Slide[] = [];
  for (const entry of children(list, "sldId")) {
    const rel = order.get(relId(entry, "id"));
    if (rel?.type !== "slide") throw new Error("Missing slide relationship");
    const root = await readXml(rel.path);
    const tree = child(child(root, "cSld"), "spTree");
    if (!tree) throw new Error("Missing slide content");
    const assets = await relationships(rel.path);
    const slide: Slide = { title: "", blocks: [] };
    // Walk the shape tree in reading order without recursion (groups may be nested).
    const shapes = children(tree).reverse();
    while (shapes.length) {
      const shape = shapes.pop()!;
      switch (shape.localName) {
        case "grpSp":
          for (const nested of children(shape).reverse()) shapes.push(nested);
          break;
        case "sp": {
          const text = paragraphs(child(shape, "txBody"));
          if (!text.length) break;
          const placeholder = child(child(child(shape, "nvSpPr"), "nvPr"), "ph")?.getAttribute("type");
          if (!slide.title && (placeholder === "title" || placeholder === "ctrTitle")) slide.title = text.map((p) => p.text).join("\n");
          else slide.blocks.push({ kind: "text", paragraphs: text });
          break;
        }
        case "graphicFrame": {
          const table = child(child(child(shape, "graphic"), "graphicData"), "tbl");
          if (table) slide.blocks.push({ kind: "table", rows: children(table, "tr").map((row) => children(row, "tc").map((cell) => paragraphs(child(cell, "txBody")).map((p) => p.text).join("\n"))) });
          break;
        }
        case "pic": {
          const asset = assets.get(relId(child(child(shape, "blipFill"), "blip"), "embed"));
          if (asset?.type !== "image") break;
          const mime = RASTER[asset.path.split(".").at(-1)?.toLowerCase() ?? ""];
          if (!mime) break;
          let src = images.get(asset.path);
          if (!src) {
            const data = await zip.file(asset.path)?.async("base64");
            if (!data) break;
            src = `data:image/${mime};base64,${data}`;
            images.set(asset.path, src);
          }
          const props = child(child(shape, "nvPicPr"), "cNvPr");
          slide.blocks.push({ kind: "picture", src, alt: props?.getAttribute("descr") ?? "" });
          break;
        }
      }
    }
    slides.push(slide);
  }
  return slides;
}
