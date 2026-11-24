import { useEffect, useRef, useState } from "react";
import { FolderPlus, X } from "lucide-react";
import { useStore, type DialogSpec } from "../store";
import type { AttachmentView } from "../types";
import { useImageMenu } from "./ImageMenu";
import { CARRY_HOW, CARRY_KIND, carryOverage } from "../lib/carry";
import { formatBytes } from "../lib/format";
import { CHATGPT_USAGE_URL, chatgptReady } from "../lib/chatgpt";
import { LogoVertical } from "./Brand";
import { SUGGESTIONS } from "../lib/vocab";
import { TextInput } from "./formkit";

/** What shows above the first message box while no conversation is open. */
export function EmptyPage() {
  const none = useStore((s) => s.projects.length === 0);
  const addProject = useStore((s) => s.addProject);
  const setLandingDraft = useStore((s) => s.setLandingDraft);
  return (
    <div className="stage">
      <div className="emptypage">
        <LogoVertical width={150} className="empty-logo" />
        <h1>WittyCode</h1>
        <p className="empty-hint">{none ? "先添加一个项目文件夹，再在下面输入消息。" : "在下面输入消息就开始；也可以在左侧打开以前的会话。"}</p>
        {none ? (
          <div className="empty-actions">
            <button type="button" className="btn primary lg" onClick={addProject}>
              <FolderPlus size={15} /> 添加项目文件夹…
            </button>
          </div>
        ) : (
          <div className="suggest">
            {SUGGESTIONS.map((text) => (
              <button key={text} type="button" className="chip-suggest" onClick={() => setLandingDraft(text)}>
                {text}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** The attachments of a conversation that changes engine, one checkbox each. Nothing is ticked at first, an attachment
 *  that cannot go is shown with its reason and cannot be ticked. A pick that is more than one message usually holds says
 *  by how much, and the button says it is over: the user decides, nothing is dropped or refused for it (only past the
 *  safety ceiling does the button stay off). The page never drops or adds an attachment on its own. */
function CarryList({ dialog, carry }: { dialog: DialogSpec; carry: NonNullable<DialogSpec["carry"]> }) {
  const openDialog = useStore((s) => s.openDialog);
  const { choice } = carry;
  const [picked, setPicked] = useState<string[]>([]);
  const { over, blocked } = carryOverage(choice, picked);
  const possible = choice.attachments.filter((a) => a.as);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const confirm = () => {
    if (blocked) return;
    openDialog(null);
    carry.onPick(picked, over !== "");
  };
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && openDialog(null)}>
      <div className="dialog wide" role="dialog" aria-modal="true" aria-label={dialog.title}>
        <h2>{dialog.title}</h2>
        <p>{dialog.body}</p>
        <ul className="carry-list">
          {choice.attachments.map((a) => (
            <li key={a.id} className={a.as ? "" : "unavailable"}>
              <label>
                <input type="checkbox" checked={picked.includes(a.id)} disabled={!a.as} onChange={() => toggle(a.id)} />
                <span className="carry-kind">{CARRY_KIND[a.kind]}</span>
                <span className="carry-name" title={a.name}>
                  {a.name}
                </span>
                <span className="carry-how">{a.as ? `${CARRY_HOW[a.as]} · ${formatBytes(a.bytes)}` : "带不过去"}</span>
              </label>
              {a.reason && <div className="carry-reason">{a.reason}</div>}
            </li>
          ))}
        </ul>
        <div className="carry-all">
          <button type="button" className="linkbtn" disabled={possible.length === 0} onClick={() => setPicked(possible.map((a) => a.id))}>
            全选能带的
          </button>
          <button type="button" className="linkbtn" disabled={picked.length === 0} onClick={() => setPicked([])}>
            全不选
          </button>
        </div>
        {blocked && <div className="carry-problem" role="alert">{blocked}</div>}
        {over && (
          <div className="carry-over" role="alert">
            {over}。超出了也可以带，由你决定。
          </div>
        )}
        <div className="dialog-btns">
          <button type="button" className="btn" onClick={() => openDialog(null)} autoFocus>
            取消
          </button>
          <button type="button" className="btn primary" disabled={!!blocked} onClick={confirm}>
            {picked.length ? `${over ? "超出了，仍然" : ""}带上选中的 ${picked.length} 个，换过去` : "都不带，换过去"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** A dialog that asks for a line of text (Enter or the button hands it over). */
function InputDialog({ dialog, input }: { dialog: DialogSpec; input: NonNullable<DialogSpec["input"]> }) {
  const openDialog = useStore((s) => s.openDialog);
  const [value, setValue] = useState(input.value);
  const submit = () => {
    openDialog(null);
    input.onSubmit(value);
  };
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && openDialog(null)}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={dialog.title}>
        <h2>{dialog.title}</h2>
        <p>{dialog.body}</p>
        <TextInput value={value} onChange={setValue} label={dialog.title} placeholder={input.placeholder} autoFocus width="100%" onEnter={submit} />
        <div className="dialog-btns">
          <button type="button" className="btn" onClick={() => openDialog(null)}>
            取消
          </button>
          <button type="button" className="btn primary" onClick={submit}>
            {dialog.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DialogHost() {
  const dialog = useStore((s) => s.dialog);
  const openDialog = useStore((s) => s.openDialog);
  useEffect(() => {
    if (!dialog) return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") openDialog(null);
      else if (e.key === "Enter" && !dialog.carry && !dialog.noEnter && !dialog.input) {
        const d = dialog;
        openDialog(null);
        d.onConfirm();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [dialog, openDialog]);
  if (!dialog) return null;
  if (dialog.carry) return <CarryList key={dialog.title} dialog={dialog} carry={dialog.carry} />;
  if (dialog.input) return <InputDialog key={dialog.title} dialog={dialog} input={dialog.input} />;
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && openDialog(null)}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={dialog.title}>
        <h2>{dialog.title}</h2>
        <p>{dialog.body}</p>
        <div className="dialog-btns">
          <button type="button" className="btn" onClick={() => openDialog(null)} autoFocus>
            取消
          </button>
          <button
            type="button"
            className={`btn ${dialog.danger ? "danger-solid" : "primary"}`}
            onClick={() => {
              const d = dialog;
              openDialog(null);
              d.onConfirm();
            }}
          >
            {dialog.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** OpenAI's UI guideline for apps that use a ChatGPT plan: the first time someone signs in with it, say that eligible requests
 *  now use their plan and where to manage that; shown once (`welcomed`), dismissed with "Got it". */
export function ChatGPTWelcome() {
  const chatgpt = useStore((s) => s.chatgpt);
  const welcome = useStore((s) => s.chatgptWelcome);
  const showing = chatgptReady(chatgpt) && !chatgpt.welcomed;
  useEffect(() => {
    if (!showing) return;
    const key = (e: KeyboardEvent) => (e.key === "Escape" || e.key === "Enter") && void welcome();
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [showing, welcome]);
  if (!showing) return null;
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && void welcome()}>
      <div className="dialog chatgpt-welcome" role="dialog" aria-modal="true" aria-label="You're using your ChatGPT Plan">
        <h2>You're using your ChatGPT Plan</h2>
        <p>
          ChatGPT Plan 会话里符合条件的请求，会用你 ChatGPT 套餐已含的额度，不会另外收费。可以在 ChatGPT 设置里查看用量，并给 WittyCode 设上限：
          <a className="inline-link" href={CHATGPT_USAGE_URL}>
            Manage usage
          </a>
          。
        </p>
        <div className="dialog-btns">
          <button type="button" className="btn primary" onClick={() => void welcome()} autoFocus>
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}

const FIT_PICTURE = { scale: 1, x: 0, y: 0 };

function Lightbox({ a, close }: { a: AttachmentView; close: () => void }) {
  const [view, setView] = useState(FIT_PICTURE);
  const [panning, setPanning] = useState(false);
  const scrim = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; x: number; y: number } | null>(null);
  const { show, menu } = useImageMenu();
  const reset = () => setView(FIT_PICTURE);

  useEffect(() => {
    const node = scrim.current;
    if (!node) return;
    const zoom = (e: WheelEvent) => {
      e.preventDefault();
      const r = frame.current?.getBoundingClientRect();
      if (!r || !e.deltaY) return;
      const x = e.clientX - (r.left + r.width / 2);
      const y = e.clientY - (r.top + r.height / 2);
      const delta = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? node.clientHeight : 1);
      setView((prev) => {
        const scale = Math.min(8, Math.max(0.2, prev.scale * Math.exp(-delta * 0.002)));
        if (scale === prev.scale) return prev;
        // The point in the image under the pointer stays under it after the scale changes.
        const ratio = scale / prev.scale;
        return { scale, x: x - (x - prev.x) * ratio, y: y - (y - prev.y) * ratio };
      });
    };
    // A non-passive listener keeps both the page and Chromium's own zoom still.
    node.addEventListener("wheel", zoom, { passive: false });
    return () => node.removeEventListener("wheel", zoom);
  }, []);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "0" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        reset();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [close]);

  return (
    <div ref={scrim} className="scrim lightbox" role="dialog" aria-modal="true" aria-label={a.name}
      onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <figure>
        <div className="lb-image-frame" ref={frame}>
          <img src={a.url} alt={a.name} draggable={false} data-pannable={view.scale > 1 ? "" : undefined}
            data-panning={panning ? "" : undefined} style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }}
            onContextMenu={(e) => show(e)} onDoubleClick={reset}
            onPointerDown={(e) => {
              if (e.button !== 0 || view.scale <= 1 || drag.current) return;
              e.currentTarget.setPointerCapture(e.pointerId);
              drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY };
              setPanning(true);
            }}
            onPointerMove={(e) => {
              const p = drag.current;
              if (!p || p.id !== e.pointerId) return;
              const dx = e.clientX - p.x;
              const dy = e.clientY - p.y;
              drag.current = { id: p.id, x: e.clientX, y: e.clientY };
              setView((prev) => prev.scale > 1 ? { ...prev, x: prev.x + dx, y: prev.y + dy } : prev);
            }}
            onLostPointerCapture={(e) => {
              if (drag.current?.id !== e.pointerId) return;
              drag.current = null;
              setPanning(false);
            }} />
        </div>
        <figcaption><span>
          {a.name} · {a.width ? `${a.width}×${a.height} · ` : ""}
          {formatBytes(a.size)}
        </span></figcaption>
      </figure>
      <button type="button" className="lb-close" aria-label="关闭" onClick={close} autoFocus>
        <X size={18} />
      </button>
      {menu}
    </div>
  );
}

export function LightboxHost() {
  const a = useStore((s) => s.lightbox);
  const open = useStore((s) => s.openLightbox);
  return a && <Lightbox key={a.id} a={a} close={() => open(null)} />;
}

export function DropOverlay() {
  const dragging = useStore((s) => s.dragging);
  if (!dragging) return null;
  return (
    <div className="drop-overlay">
      <div className="drop-box">松开鼠标，把文件或图片添加为附件</div>
    </div>
  );
}
