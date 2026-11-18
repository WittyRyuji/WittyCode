import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { messageOf } from "../lib/errors";
import { readWorkbook, sheetView } from "../lib/officeSheet";
import { readSlides, type Slide } from "../lib/officeSlides";
import { readWord } from "../lib/officeWord";
import { useStore } from "../store";
import type { OfficeKind } from "../types";

type Preview = { kind: "word"; document: DocumentFragment } | { kind: "sheet"; book: ReturnType<typeof readWorkbook> } | { kind: "slides"; slides: Slide[] };
const TITLE: Record<OfficeKind, string> = { word: "Word 文档", sheet: "Excel 工作簿", slides: "PowerPoint 演示文稿" };

function decode(url: string | null): Uint8Array {
  const prefix = /^data:application\/[\w.+-]+;base64,/i.exec(url ?? "");
  if (!url || !prefix) throw new Error("No Office data");
  const binary = atob(url.slice(prefix[0].length));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < bytes.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function Word({ content }: { content: DocumentFragment }) {
  const scroll = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const box = scroll.current, el = host.current;
    if (!box || !el) return;
    const shadow = el.shadowRoot ?? el.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = `.fp-docx-wrapper { padding: 0; background: transparent; color: #171717; }
      .fp-docx-wrapper > section.fp-docx { margin-bottom: 10px; }
      .fp-docx-wrapper a[href] { cursor: pointer; color: #23569a; text-decoration: underline; }`;
    // Keep document CSS out of the application UI. Only sanitized nodes are cloned into the page.
    shadow.replaceChildren(content.cloneNode(true), style);
    const paper = shadow.querySelector<HTMLElement>(".fp-docx-wrapper");
    const fit = () => {
      if (!paper) return;
      const width = Math.max(1, ...Array.from(paper.children, (page) => (page as HTMLElement).offsetWidth));
      paper.style.zoom = String(Math.min(1, Math.max(1, box.clientWidth - 24) / width));
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(box);
    const follow = (event: Event) => {
      const target = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!target) return;
      event.preventDefault(); // live.ts routes approved external links, including links inside this shadow root.
      const href = target.getAttribute("href");
      if (href?.startsWith("#")) shadow.getElementById(href.slice(1))?.scrollIntoView({ block: "nearest" });
    };
    shadow.addEventListener("click", follow);
    return () => {
      observer.disconnect();
      shadow.removeEventListener("click", follow);
      shadow.replaceChildren();
    };
  }, [content]);
  return <div className="fp-word" ref={scroll}><div className="fp-word-paper selectable" ref={host} /></div>;
}

function Sheet({ book }: { book: ReturnType<typeof readWorkbook> }) {
  const [selected, setSelected] = useState(0);
  const [page, setPage] = useState(0);
  const name = book.SheetNames[selected];
  const sheet = sheetView(book, name, page);
  return <>
    <div className="fp-tabs" aria-label="工作表">
      {book.SheetNames.map((title, i) => <button type="button" key={i} className="linkbtn" aria-pressed={selected === i} onClick={() => { setSelected(i); setPage(0); }}>{title}</button>)}
    </div>
    {sheet?.truncated && <div className="fp-note">工作表太大，只显示了前面一部分。想看全部，用默认程序打开。</div>}
    {!sheet ? <div className="fp-state">这个工作表是空的。</div> : <>
      <div className="fp-grid fp-scroll selectable" key={`${selected}:${page}`}>
        <table aria-label={name}>
          <thead><tr><th aria-label="行号" />{sheet.columns.map((column) => <th scope="col" key={column}>{column}</th>)}</tr></thead>
          <tbody>{sheet.rows.map(({ number, cells }) => <tr key={number}>
            <th scope="row">{number}</th>
            {cells.map(({ column, text, width, height }) => <td key={column} colSpan={width} rowSpan={height}>{text}</td>)}
          </tr>)}</tbody>
        </table>
      </div>
      {sheet.pages > 1 && <div className="fp-grid-nav">
        <button type="button" className="linkbtn" disabled={!page} onClick={() => setPage(page - 1)}>上一组</button>
        <span>{page + 1} / {sheet.pages}</span>
        <button type="button" className="linkbtn" disabled={page + 1 >= sheet.pages} onClick={() => setPage(page + 1)}>下一组</button>
      </div>}
    </>}
  </>;
}

function Slides({ slides }: { slides: Slide[] }) {
  if (!slides.length) return <div className="fp-state">这个演示文稿没有幻灯片。</div>;
  return <div className="fp-slides fp-scroll selectable">{slides.map(({ title, blocks }, i) => <section className="fp-slide" key={i} aria-label={`第 ${i + 1} 张幻灯片`}>
    <div className="fp-slide-num">第 {i + 1} 张</div>
    {title && <h3>{title}</h3>}
    {blocks.map((block, n) => block.kind === "picture" ? <img src={block.src} alt={block.alt} key={n} loading="lazy" /> : block.kind === "table" ?
      <div className="fp-slide-table" key={n}><table><tbody>{block.rows.map((row, r) => <tr key={r}>{row.map((value, c) => <td key={c}>{value}</td>)}</tr>)}</tbody></table></div> :
      <div key={n}>{block.paragraphs.map(({ text, bullet, level }, p) => <p key={p} style={{ paddingInlineStart: `${level * 0.85}em` }}>{bullet && <span aria-hidden>• </span>}{text}</p>)}</div>)}
  </section>)}</div>;
}

export function OfficePreview({ kind, fileRef, size, renderError }: { kind: OfficeKind; fileRef: string; size: string; renderError: (message: string) => ReactNode }) {
  const filePage = useStore((s) => s.filePage);
  const [result, setResult] = useState<Preview | string | null>(null);
  useEffect(() => {
    let current = true;
    async function load() {
      let url;
      try { url = await filePage(fileRef, 0, 0, true); }
      catch (error) { if (current) setResult(messageOf(error)); return; }
      if (!current) return;
      try {
        const bytes = decode(url);
        const preview: Preview = kind === "word" ? { kind, document: await readWord(bytes) } : kind === "sheet" ? { kind, book: readWorkbook(bytes) } : { kind, slides: await readSlides(bytes) };
        if (current) setResult(preview);
      } catch {
        if (current) setResult("这个文件画不出来：用默认程序打开。");
      }
    }
    void load();
    return () => { current = false; };
  }, [kind, fileRef, filePage]);
  const label = typeof result === "object" && result?.kind === "sheet" ? `${result.book.SheetNames.length} 个工作表` :
    typeof result === "object" && result?.kind === "slides" ? `${result.slides.length} 张幻灯片` : TITLE[kind];
  return <>
    <div className="fp-facts">{label} · {size}</div>
    {result === null ? <div className="fp-state"><Loader2 size={15} className="spin" /> 正在读取…</div> : typeof result === "string" ? renderError(result) :
      result.kind === "word" ? <Word content={result.document} /> : result.kind === "sheet" ? <Sheet book={result.book} /> : <Slides slides={result.slides} />}
  </>;
}
