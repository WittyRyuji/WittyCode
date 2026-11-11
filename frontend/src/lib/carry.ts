import type { CarryItem, HandoffChoice } from "../protocol";

// Type imports only: scripts/unit.mjs runs this file in plain Node, which cannot resolve extensionless imports.

const MIB = 1024 * 1024;
/** Past this a pick is refused, not asked about (core/attachments.HARD_*): the page only has it for older Pythons. */
const HARD_DEFAULT = { files: 50, bytes: 200 * MIB };

/** What an attachment is called in the list, and how it will travel when ticked. */
export const CARRY_KIND: Record<CarryItem["kind"], string> = { text: "文本", image: "图片", pdf: "PDF" };
export const CARRY_HOW: Record<Exclude<CarryItem["as"], "">, string> = {
  text: "文字随对话一起交给它",
  image: "图片随下一条消息发出",
  pdf: "PDF 文件随下一条消息发出",
};

/** The size as Python words it (core/attachments.format_size), so the page and Python say the same sentence. */
const size = (n: number): string => (n < 1024 ? `${n} B` : n < MIB ? `${(n / 1024).toFixed(1)} KB` : `${(n / MIB).toFixed(1)} MB`);

export interface CarryOverage {
  /** "": the first message holds no more than a message usually does. Else by how much it is over (core/attachments.Overage.describe). */
  over: string;
  /** "": fine. Else why this much cannot go at all (past the safety ceiling), and the pick is not offered. */
  blocked: string;
}

/** Pictures and PDF files travel as files, with the next message; one message usually holds 10 of them and 20 MB. More is
 *  the user's call: the page says by how much (`over`) and the user confirms, nothing is refused or dropped for it. What
 *  the message box already holds goes with that message too and counts. Only past the safety ceiling (`hard`) is it
 *  `blocked`. The same rule, and the same words, as core/handoff.carry_overage. */
export function carryOverage(choice: HandoffChoice, picked: readonly string[]): CarryOverage {
  const chosen = new Set(picked);
  const files = choice.attachments.filter((a) => chosen.has(a.id) && (a.as === "image" || a.as === "pdf"));
  if (files.length === 0) return { over: "", blocked: "" }; // nothing of this pick travels as a file: the send's own matter
  const inBox = choice.drafts ?? { count: 0, bytes: 0 };
  const count = files.length + inBox.count;
  const total = files.reduce((sum, a) => sum + a.bytes, 0) + inBox.bytes;
  const hard = choice.hard ?? HARD_DEFAULT;
  if (count > hard.files || total > hard.bytes) {
    return {
      over: "",
      blocked: `附件太多了：一条消息最多 ${hard.files} 个、${Math.floor(hard.bytes / MIB)} MB，现在有 ${count} 个、${size(total)}：再多多半是误操作，请分几条消息发。`,
    };
  }
  const parts: string[] = [];
  if (count > choice.limits.files) parts.push(`多 ${count - choice.limits.files} 个文件`);
  if (total > choice.limits.bytes) parts.push(`多 ${size(total - choice.limits.bytes)}`);
  if (parts.length === 0) return { over: "", blocked: "" };
  return {
    over: `比一条消息通常带的量${parts.join("、")}（这条消息有 ${count} 个附件、${size(total)}；通常最多 ${choice.limits.files} 个、${Math.floor(choice.limits.bytes / MIB)} MB）`,
    blocked: "",
  };
}
