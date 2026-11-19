import { read, utils, type WorkBook } from "xlsx";

export const SHEET_PAGE_ROWS = 100;
const ROWS = 2_000;
const COLUMNS = 200;

export function readWorkbook(bytes: Uint8Array): WorkBook {
  const book = read(bytes, { type: "array", cellFormula: false, cellHTML: false, bookVBA: false });
  if (!book.SheetNames.length) throw new Error("No sheets");
  return book;
}

export function sheetView(book: WorkBook, name: string, page: number) {
  const sheet = book.Sheets[name];
  const ref = sheet?.["!ref"];
  let filled = false;
  for (const key in sheet) if (key[0] !== "!") { filled = true; break; }
  if (!ref || !filled) return null;
  const { s, e } = utils.decode_range(ref);
  if (![s.r, s.c, e.r, e.c].every((n) => Number.isInteger(n) && n >= 0) || e.r < s.r || e.c < s.c) return null;
  const count = Math.min(e.r - s.r + 1, ROWS);
  const lastCol = Math.min(e.c, s.c + COLUMNS - 1);
  const firstRow = s.r + page * SHEET_PAGE_ROWS;
  const lastRow = Math.min(firstRow + SHEET_PAGE_ROWS - 1, s.r + count - 1);
  const columns = Array.from({ length: lastCol - s.c + 1 }, (_, i) => utils.encode_col(s.c + i));
  const pages = Math.ceil(count / SHEET_PAGE_ROWS);
  const truncated = e.r - s.r + 1 > ROWS || e.c > lastCol;
  const merges = new Map<string, { width: number; height: number; text: string } | null>();
  const textAt = (r: number, c: number): string => {
    const cell = sheet[utils.encode_cell({ r, c })];
    return cell?.w ?? (cell?.v == null ? "" : String(cell.v));
  };
  for (const { s: start, e: end } of sheet["!merges"] ?? []) {
    const top = Math.max(start.r, firstRow), bottom = Math.min(end.r, lastRow);
    const left = Math.max(start.c, s.c), right = Math.min(end.c, lastCol);
    if (top > bottom || left > right) continue;
    // Ignore overlapping malformed ranges instead of writing over an earlier cell.
    if (merges.has(`${top},${left}`)) continue;
    for (let r = top; r <= bottom; r++) {
      for (let c = left; c <= right; c++) {
        const key = `${r},${c}`;
        if (!merges.has(key)) merges.set(key, r === top && c === left ? { width: right - left + 1, height: bottom - top + 1, text: textAt(start.r, start.c) } : null);
      }
    }
  }
  const rows = [];
  for (let r = firstRow; r <= lastRow; r++) {
    const cells = [];
    for (let c = s.c; c <= lastCol; c++) {
      const merge = merges.get(`${r},${c}`);
      if (merge !== null) cells.push({ column: c, ...(merge ?? { width: 1, height: 1, text: textAt(r, c) }) });
    }
    rows.push({ number: r + 1, cells });
  }
  return { columns, rows, pages, truncated };
}
