import { useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { ClipboardCopy } from "lucide-react";
import { liveCall } from "../liveBridge";
import { useStore } from "../store";
import { Menu, type Anchor } from "./ui";

async function asPng(img: HTMLImageElement, full?: { id?: string; attachmentId: string }): Promise<Blob> {
  if (full) {
    // Saved attachments may only have a thumbnail in the transcript. Use the same image as the viewer.
    const result = await liveCall("attachments.full", full).catch(() => null);
    if (result?.url) {
      img = new Image();
      img.src = result.url;
    }
  }
  await img.decode();
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("No image canvas");
  ctx.drawImage(img, 0, 0);
  return new Promise((resolve, reject) => canvas.toBlob((blob) => {
    if (blob) resolve(blob);
    else reject(new Error("Could not encode image"));
  }, "image/png"));
}

export function useImageMenu() {
  const [source, setSource] = useState<{ anchor: Anchor; img: HTMLImageElement; full?: { id?: string; attachmentId: string } } | null>(null);
  const say = useStore((s) => s.say);
  const show = (e: MouseEvent<HTMLElement>, attachmentId?: string) => {
    const target = e.currentTarget;
    const img = target instanceof HTMLImageElement ? target : target.querySelector("img");
    if (!img?.naturalWidth) return;
    e.preventDefault();
    e.stopPropagation();
    setSource({ anchor: { x: e.clientX, y: e.clientY }, img,
      full: attachmentId ? { id: useStore.getState().currentId ?? undefined, attachmentId } : undefined });
  };
  const copy = async () => {
    if (!source) return;
    try {
      // Pass the pending PNG to the clipboard during the click, without losing the user gesture.
      await navigator.clipboard.write([new ClipboardItem({ "image/png": asPng(source.img, source.full) })]);
    } catch {
      say("没能将图片复制到剪贴板。");
    }
  };
  const menu = source && createPortal(
    <div className="image-menu-layer">
      <Menu anchor={source.anchor} onClose={() => setSource(null)} minWidth={140}
        items={[{ id: "copy-image", label: "复制图片", icon: <ClipboardCopy size={14} />, onSelect: () => void copy() }]} />
    </div>, document.body,
  );
  return { show, menu };
}
