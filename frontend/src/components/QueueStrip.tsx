import { useLayoutEffect, useRef, useState } from "react";
import { Check, Eye, EyeOff, Paperclip, Pencil, SendHorizontal, X } from "lucide-react";
import { useStore } from "../store";
import type { QueuedMessage, Session } from "../types";
import { Tip } from "./ui";

function IconButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: React.ReactNode }) {
  return (
    <Tip text={label} side="top">
      <button type="button" className="icon-btn sm" aria-label={label} disabled={disabled} onMouseDown={(e) => e.preventDefault()} onClick={onClick}>
        {children}
      </button>
    </Tip>
  );
}

function Row({ session, item, index, count }: { session: Session; item: QueuedMessage; index: number; count: number }) {
  const unqueue = useStore((s) => s.unqueue);
  const editQueued = useStore((s) => s.editQueued);
  const sendQueued = useStore((s) => s.sendQueued);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null); // the words being edited; null: not editing
  const editing = draft !== null;
  const box = useRef<HTMLTextAreaElement>(null);
  // The editor is as tall as its words (a few lines at most), not a line with an arrow to scroll it.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    const wanted = el.scrollHeight + (el.offsetHeight - el.clientHeight); // (its border too)
    el.style.height = `${Math.min(wanted, 120)}px`;
    el.style.overflowY = wanted > 120 ? "auto" : "hidden";
  }, [draft]);
  const connecting = session.mark === "opening";
  const wait = index === 0 ? (connecting ? "连上就发出" : "本轮结束后发出") : "等前面的发完";
  const save = () => {
    if (draft === null) return;
    if (draft.trim() !== item.text.trim()) editQueued(session.id, item.id, draft);
    setDraft(null);
  };
  return (
    <div className={`qs-row${open ? " open" : ""}${editing ? " editing" : ""}`} role="listitem">
      <span className="qs-tag" title={wait}>
        {count > 1 ? `排队 ${index + 1}/${count}` : "排队"}
      </span>
      {editing ? (
        <textarea
          className="qs-edit selectable"
          autoFocus
          ref={box}
          rows={1}
          value={draft}
          aria-label="修改排队的消息"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              save();
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setDraft(null);
            }
          }}
        />
      ) : (
        <span className="qs-text selectable">{item.text || "（只有附件）"}</span>
      )}
      {item.attachments.length > 0 && !editing && (
        <span className="qs-att" title={item.attachments.map((a) => a.name).join("、")}>
          <Paperclip size={12} />
          {item.attachments.length}
        </span>
      )}
      <span className="qs-icons">
        {editing ? (
          <>
            <IconButton label="保存修改（Enter）" onClick={save}>
              <Check size={15} />
            </IconButton>
            <IconButton label="放弃修改（Esc）" onClick={() => setDraft(null)}>
              <X size={15} />
            </IconButton>
          </>
        ) : (
          <>
            <IconButton label={open ? "收起" : "查看全文"} onClick={() => setOpen((v) => !v)}>
              {open ? <EyeOff size={15} /> : <Eye size={15} />}
            </IconButton>
            <IconButton label="修改" onClick={() => setDraft(item.text)}>
              <Pencil size={15} />
            </IconButton>
            <IconButton label="取消排队（放回输入框）" onClick={() => unqueue(session.id, item.id)}>
              <X size={15} />
            </IconButton>
            <IconButton
              label={connecting ? "还没连上，连上就会发出" : "立即发送（交给正在运行的这一轮）"}
              disabled={connecting}
              onClick={() => sendQueued(session.id, item.id)}
            >
              <SendHorizontal size={15} />
            </IconButton>
          </>
        )}
      </span>
    </div>
  );
}

/** Messages typed while the session was connecting or a turn was running, one line each above the box they came from: they
 *  go out by themselves, in order, each when the one before it is over. At the end of the line: see all of it, change it,
 *  take it back, send it now. */
export function QueueStrip({ session }: { session: Session }) {
  const queued = session.queued;
  if (!queued || queued.length === 0) return null;
  return (
    <div className="queue-strip" role="list" aria-label="排队的消息">
      {queued.map((item, index) => (
        <Row key={item.id} session={session} item={item} index={index} count={queued.length} />
      ))}
    </div>
  );
}
