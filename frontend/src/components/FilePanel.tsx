import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AppWindow, ClipboardCopy, ExternalLink, FileText, FolderOpen, Loader2, X } from "lucide-react";
import { currentSession, useStore } from "../store";
import type { FileView } from "../types";
import { highlight } from "../lib/highlight";
import { Markdown } from "./blocks/Markdown";
import { Menu, type MenuItem } from "./ui";

/** Height of one line of the code view: the highlight of the named lines is placed by it. */
const LINE = 20;
const PAD = 8;

function size(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function Code({ file }: { file: FileView }) {
  const box = useRef<HTMLDivElement>(null);
  const lines = useMemo(() => file.text.split("\n"), [file.text]);
  const numbers = useMemo(() => lines.map((_, i) => i + 1).join("\n"), [lines]);
  // (highlighting keeps every newline, so the lines stay where the gutter counts them)
  const html = useMemo(() => (file.text.length <= 300_000 ? highlight(file.text, /\.([\w+-]+)$/.exec(file.name)?.[1] ?? "") : null), [file.text, file.name]);
  const first = file.line;
  const last = file.lineEnd ?? file.line;
  useLayoutEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = first ? Math.max(0, (first - 1) * LINE - 3 * LINE) : 0;
  }, [file.path, first]);
  return (
    <div ref={box} className="fp-scroll">
      <div className="fp-rows">
        {first && last && first <= lines.length && <div className="fp-mark" style={{ top: PAD + (first - 1) * LINE, height: (Math.min(last, lines.length) - first + 1) * LINE }} />}
        <pre className="fp-gutter" aria-hidden>
          {numbers}
        </pre>
        <pre className="fp-text selectable">{html !== null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : file.text}</pre>
      </div>
    </div>
  );
}

/** The width Python draws a page at: the panel's own width in device pixels, in steps so that a small change does not matter. */
function drawWidth(el: HTMLElement | null): number {
  const wide = (el?.clientWidth ?? 600) * (window.devicePixelRatio || 1);
  return Math.min(1800, Math.max(700, Math.ceil(wide / 100) * 100));
}

/** The pages of a PDF, stacked. Python draws them; each is asked for when it comes near the visible part. */
function Pdf({ file, fileRef }: { file: FileView; fileRef: string }) {
  const filePage = useStore((s) => s.filePage);
  const box = useRef<HTMLDivElement>(null);
  // (undefined: not asked for yet, null: could not be drawn)
  const [urls, setUrls] = useState<Record<number, string | null>>({});
  const [current, setCurrent] = useState(0);
  const asked = useRef(new Set<number>());
  const width = useRef(0);
  const request = useCallback(
    (n: number) => {
      asked.current.add(n);
      void filePage(fileRef, n, width.current).then((url) => setUrls((u) => ({ ...u, [n]: url })));
    },
    [fileRef, filePage],
  );
  useEffect(() => {
    const root = box.current;
    if (!root) return;
    width.current = drawWidth(root);
    const watcher = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const n = Number((entry.target as HTMLElement).dataset.page);
          if (entry.isIntersecting && !asked.current.has(n)) request(n);
        }
      },
      { root, rootMargin: "900px 0px" },
    );
    root.querySelectorAll(".fp-sheet").forEach((sheet) => watcher.observe(sheet));
    return () => watcher.disconnect();
  }, [file.path, request]);
  const onScroll = () => {
    const root = box.current;
    if (!root) return;
    const sheets = Array.from(root.querySelectorAll<HTMLElement>(".fp-sheet"));
    const line = root.scrollTop + root.clientHeight * 0.35;
    let at = 0;
    sheets.forEach((sheet, i) => {
      if (sheet.offsetTop <= line) at = i;
    });
    setCurrent(at);
  };
  return (
    <div className="fp-pdf">
      <div ref={box} className="fp-pages" onScroll={onScroll}>
        {file.pages.map((p, i) => (
          <div key={i} className="fp-sheet" data-page={i} style={{ aspectRatio: `${p.w} / ${p.h}` }} aria-label={`第 ${i + 1} 页`}>
            {urls[i] ? (
              <img src={urls[i]!} alt={`第 ${i + 1} 页`} draggable={false} />
            ) : urls[i] === null ? (
              <button type="button" className="fp-wait linkbtn" onClick={() => request(i)}>
                这一页画不出来，点一下再试
              </button>
            ) : (
              <div className="fp-wait">正在画…</div>
            )}
          </div>
        ))}
      </div>
      {file.pages.length > 1 && (
        <div className="fp-pagepill" aria-live="off">
          {current + 1} / {file.pages.length}
        </div>
      )}
    </div>
  );
}

/** A picture: fitted to the panel, a click shows it at its own size. */
function Picture({ file, fileRef }: { file: FileView; fileRef: string }) {
  const filePage = useStore((s) => s.filePage);
  const [url, setUrl] = useState<string | null | undefined>(undefined);
  const [actual, setActual] = useState(false);
  useEffect(() => {
    let current = true;
    setUrl(undefined);
    setActual(false);
    void filePage(fileRef, 0, 0).then((u) => current && setUrl(u));
    return () => {
      current = false;
    };
  }, [file.path, fileRef, filePage]);
  if (url === undefined) return <div className="fp-state"><Loader2 size={15} className="spin" /> 正在读取…</div>;
  if (url === null) return <div className="fp-state">这张图片画不出来：用默认程序打开。</div>;
  return (
    <div className={`fp-picture${actual ? " actual" : ""}`}>
      <img src={url} alt={file.name} draggable={false} onClick={() => setActual(!actual)} title={actual ? "点击适应窗口" : "点击看原始大小"} />
    </div>
  );
}

/** The right-hand panel for a file the conversation named: a text file's text (a Markdown file as a document, with the source a
 *  click away; the lines the reference named marked), the pages of a PDF, a picture, and the other ways to open the file. */
export function FilePanel() {
  const panel = useStore((s) => s.filePanel);
  const session = useStore(currentSession);
  const close = useStore((s) => s.closeFile);
  const openFile = useStore((s) => s.openFile);
  const copyPath = useStore((s) => s.copyPath);
  // (null: not chosen yet. A reference to lines is about the lines of the source, so a Markdown file opens as source then.)
  const [chosen, setSource] = useState<boolean | null>(null);
  const shown = !!panel && !!session && session.id === panel.sessionId;
  useEffect(() => setSource(null), [panel?.ref]);
  const source = chosen ?? !!panel?.file?.line;
  useEffect(() => {
    if (!shown) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.querySelector(".menu") && close();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [shown, close]);
  if (!panel || !shown) return null;
  const file = panel.file;
  const title = file?.name ?? panel.ref.replace(/:\d+(-\d+)?$/, "").split(/[\\/]/).pop() ?? panel.ref;
  return (
    <aside className="filepanel" aria-label={`文件 ${title}`}>
      <header className="fp-head">
        <FileText size={16} className="fp-icon" />
        <div className="fp-title">
          <div className="fp-name selectable">{title}</div>
          {file && (
            <div className="fp-dir" title={file.path}>
              {file.dir}
            </div>
          )}
        </div>
        <button type="button" className="icon-btn sm" title="用默认程序打开" aria-label="用默认程序打开" onClick={() => openFile(panel.ref, "default")}>
          <ExternalLink size={15} />
        </button>
        <button type="button" className="icon-btn sm" title="选择打开方式…" aria-label="选择打开方式" onClick={() => openFile(panel.ref, "choose")}>
          <AppWindow size={15} />
        </button>
        <button type="button" className="icon-btn sm" title="在文件夹中显示" aria-label="在文件夹中显示" onClick={() => openFile(panel.ref, "reveal")}>
          <FolderOpen size={15} />
        </button>
        <button type="button" className="icon-btn sm" title="复制路径" aria-label="复制路径" onClick={() => copyPath(panel.ref)}>
          <ClipboardCopy size={15} />
        </button>
        <button type="button" className="icon-btn sm" title="关闭（Esc）" aria-label="关闭文件面板" onClick={close}>
          <X size={15} />
        </button>
      </header>
      {file && (
        <div className="fp-facts">
          <span>
            {file.kind === "pdf" ? `${file.pages.length.toLocaleString()} 页 · ${size(file.size)}` : file.kind === "image" ? size(file.size) : `${file.lines.toLocaleString()} 行 · ${size(file.size)} · ${file.encoding}`}
          </span>
          {file.line && (
            <span>
              第 {file.line}
              {file.lineEnd ? `–${file.lineEnd}` : ""} 行
            </span>
          )}
          {file.markdown && (
            <button type="button" className="linkbtn" onClick={() => setSource(!source)}>
              {source ? "看文档" : "看源码"}
            </button>
          )}
        </div>
      )}
      {file?.truncated && <div className="fp-note">文件太长，只显示了前面一部分。想看全部，用默认程序打开。</div>}
      <div className="fp-body">
        {panel.status === "loading" && (
          <div className="fp-state">
            <Loader2 size={15} className="spin" /> 正在读取…
          </div>
        )}
        {panel.status === "error" && (
          <div className="fp-state fp-error" role="alert">
            <div>{panel.error}</div>
            <div className="fp-actions">
              <button type="button" className="btn small" onClick={() => openFile(panel.ref, "default")}>
                用默认程序打开
              </button>
              <button type="button" className="btn small" onClick={() => openFile(panel.ref, "reveal")}>
                在文件夹中显示
              </button>
            </div>
          </div>
        )}
        {file?.kind === "pdf" && <Pdf key={file.path} file={file} fileRef={panel.ref} />}
        {file?.kind === "image" && <Picture file={file} fileRef={panel.ref} />}
        {file?.kind === "text" && (file.markdown && !source ? <div className="fp-doc"><Markdown source={file.text} /></div> : <Code file={file} />)}
      </div>
    </aside>
  );
}

/** The menu of a path in the conversation, opened by a right click. */
export function PathMenuHost() {
  const menu = useStore((s) => s.pathMenu);
  const hide = useStore((s) => s.hidePathMenu);
  const openFile = useStore((s) => s.openFile);
  const copyPath = useStore((s) => s.copyPath);
  if (!menu) return null;
  const items: MenuItem[] = [
    { id: "panel", label: "在右侧打开", icon: <FileText size={14} />, onSelect: () => openFile(menu.ref, "panel") },
    { id: "default", label: "用默认程序打开", shortcut: "Ctrl+点击", icon: <ExternalLink size={14} />, onSelect: () => openFile(menu.ref, "default") },
    { id: "choose", label: "选择打开方式…", icon: <AppWindow size={14} />, onSelect: () => openFile(menu.ref, "choose") },
    { id: "reveal", label: "在文件夹中显示", icon: <FolderOpen size={14} />, onSelect: () => openFile(menu.ref, "reveal") },
    { id: "sep", separator: true },
    { id: "copy", label: "复制路径", icon: <ClipboardCopy size={14} />, onSelect: () => copyPath(menu.ref) },
  ];
  return <Menu items={items} anchor={{ x: menu.x, y: menu.y }} onClose={hide} minWidth={210} />;
}
