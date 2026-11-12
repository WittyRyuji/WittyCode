// The design mock's way of turning dropped, pasted or picked files into attachment cards: read in the page, never sent
// anywhere. The live app hands the files to Python instead (`attachments.add` in live.ts), which admits and shrinks them.

import type { AttachmentView } from "../types";

/** Reads dropped, pasted or picked files into attachment cards (the limits are WittyCode's: 5 MiB pictures, 3 MiB PDFs). */
export async function readFiles(files: File[], say: (text: string) => void): Promise<AttachmentView[]> {
  const out: AttachmentView[] = [];
  for (const file of files) {
    const id = `att-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    if (file.type.startsWith("image/")) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
        say(`「${file.name}」不是模型接受的图片格式（PNG / JPEG / GIF / WebP）。`);
        continue;
      }
      if (file.size > 5 * 1024 * 1024) {
        say(`「${file.name}」超过 5 MiB，没有添加。`);
        continue;
      }
      const url = await new Promise<string>((resolve) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result));
        r.readAsDataURL(file);
      });
      const dims = await new Promise<{ w: number; h: number }>((resolve) => {
        const img = new Image();
        img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight });
        img.onerror = () => resolve({ w: 0, h: 0 });
        img.src = url;
      });
      out.push({ id, kind: "image", name: file.name || "粘贴的图片.png", size: file.size, url, width: dims.w || undefined, height: dims.h || undefined });
    } else if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) {
      if (file.size > 3 * 1024 * 1024) {
        say(`「${file.name}」超过 3 MiB，没有添加。`);
        continue;
      }
      out.push({ id, kind: "pdf", name: file.name, size: file.size });
    } else {
      out.push({ id, kind: "text", name: file.name, size: file.size });
    }
  }
  return out;
}
