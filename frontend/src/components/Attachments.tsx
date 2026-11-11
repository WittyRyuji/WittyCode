import { X } from "lucide-react";
import type { AttachmentView } from "../types";
import { formatBytes } from "../lib/format";
import { useStore } from "../store";

const EXT: Record<AttachmentView["kind"], string> = { image: "IMG", text: "TXT", pdf: "PDF" };

/** The card of one attachment: used above the message box and inside a sent message. */
export function AttachmentCard({ a, onRemove }: { a: AttachmentView; onRemove?: () => void }) {
  const openLightbox = useStore((s) => s.openLightbox);
  const picture = a.kind === "image" && a.url;
  return (
    // The kind goes in an attribute: "att-text" is already the class of the name-and-size column.
    <div className="att" data-kind={a.kind}>
      <button
        type="button"
        className="att-main"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => picture && openLightbox(a)}
        disabled={!picture}
        title={picture ? "点击查看大图" : a.name}
      >
        {picture ? <img className="att-thumb" src={a.url} alt="" /> : <span className={`ficon ficon-${a.kind}`}>{EXT[a.kind]}</span>}
        <span className="att-text">
          <span className="fname">{a.name}</span>
          <span className="fmeta">
            {a.width ? `${a.width}×${a.height} · ` : ""}
            {formatBytes(a.size)}
            {a.note ? ` · ${a.note}` : ""}
          </span>
        </span>
      </button>
      {onRemove && (
        <button type="button" className="att-x" aria-label={`移除 ${a.name}`} onMouseDown={(e) => e.preventDefault()} onClick={onRemove}>
          <X size={13} />
        </button>
      )}
    </div>
  );
}

/** Pictures of a sent message are shown larger than the composer cards. */
export function SentAttachments({ items }: { items: AttachmentView[] }) {
  const openLightbox = useStore((s) => s.openLightbox);
  return (
    <div className="atts">
      {items.map((a) =>
        a.kind === "image" && a.url ? (
          <button key={a.id} type="button" className="att-img" onClick={() => openLightbox(a)} title={`${a.name} · 点击查看大图`}>
            <img src={a.url} alt={a.name} />
          </button>
        ) : (
          <AttachmentCard key={a.id} a={a} />
        ),
      )}
    </div>
  );
}
