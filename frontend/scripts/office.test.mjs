import assert from "node:assert/strict";
import test from "node:test";
import { DOMParser } from "@xmldom/xmldom";
import { utils, write } from "xlsx";
import { readSlides } from "../src/lib/officeSlides.ts";
import { readWorkbook, sheetView } from "../src/lib/officeSheet.ts";
import { cleanWord, safeWordCss, wordHref } from "../src/lib/officeWord.ts";
import { LITERAL, sheetFixture, slidesFixture } from "./office-fixtures.mjs";

test("Excel formats cells, displays literal strings and cached results, and never creates links or HTML", () => {
  const book = readWorkbook(sheetFixture(true));
  assert.deepEqual(book.SheetNames, ["月度汇总", "明细", "空表"]);
  const sheet = sheetView(book, "月度汇总", 0);
  assert.deepEqual(sheet.columns, ["A", "B", "C", "D"]);
  assert.deepEqual(sheet.rows[1].cells.map((c) => c.text), ["一月", "12", "75.0%", ""]);
  assert.deepEqual(sheet.rows[3].cells[1], { column: 1, width: 2, height: 1, text: "样例数据" });
  assert.deepEqual(sheet.rows[4].cells.map((c) => c.text), [LITERAL, "=1+1", "external", "5"]);
  assert.equal(sheetView(book, "明细", 0).rows[1].cells[1].text, "已完成");
  assert.equal(sheetView(book, "空表", 0), null);
});

test("Excel also reads legacy BIFF8 .xls workbooks", () => {
  const book = utils.book_new();
  utils.book_append_sheet(book, utils.aoa_to_sheet([["Legacy", 42]]), "Sample");
  const bytes = new Uint8Array(write(book, { type: "array", bookType: "biff8" }));
  assert.deepEqual(sheetView(readWorkbook(bytes), "Sample", 0).rows[0].cells.map((cell) => cell.text), ["Legacy", "42"]);
});

test("Excel treats a sheet that only has a dimension as empty", () => {
  assert.equal(sheetView({ Sheets: { Blank: { "!ref": "A1" } } }, "Blank", 0), null);
});

test("Excel caps large and sparse ranges, preserves row labels and clips merges to the current page", () => {
  const book = { Sheets: { Big: { "!ref": "B2:XFD1048576", "!merges": [{ s: { r: 99, c: 1 }, e: { r: 103, c: 2 } }], B100: { v: "Merged" } } } };
  const first = sheetView(book, "Big", 0);
  assert.equal(first.columns.length, 200);
  assert.equal(first.columns[0], "B");
  assert.equal(first.pages, 20);
  assert.equal(first.truncated, true);
  const second = sheetView(book, "Big", 1);
  assert.equal(second.rows[0].number, 102);
  assert.deepEqual(second.rows[0].cells[0], { column: 1, width: 2, height: 3, text: "Merged" });
});

test("PowerPoint follows relationship order, with bullets, table cells, literal text and only embedded raster pictures", async () => {
  const slides = await readSlides(await slidesFixture(true), new DOMParser());
  assert.deepEqual(slides.map((s) => s.title), ["本周进度", "下一步"]);
  const [text, table, picture] = slides[0].blocks;
  assert.deepEqual(text.paragraphs.map((p) => [p.level, p.bullet, p.text]), [
    [0, true, "已核对样例数据。"], [1, true, "检查了两个月份。"], [0, false, LITERAL], [0, false, "=1+1"],
  ]);
  assert.deepEqual(table.rows, [["事项", "状态"], ["样例数据", "已完成"]]);
  assert.match(picture.src, /^data:image\/png;base64,iVBOR/);
  assert.equal(slides[0].blocks.length, 3);
});

test("Word only permits approved links and base64 media, never event handlers, srcset or active elements", () => {
  for (const href of ["javascript:marker", " java\tscript:marker", "file:///tmp/demo", "data:text/html,x", "vbscript:marker", "//example.com/x", "/local"]) assert.equal(wordHref(href), null);
  for (const href of ["https://example.com/a", "http://example.com/a", "mailto:demo@example.com", "#mark"]) assert.equal(wordHref(href), href);
  const markup = `<main xmlns="http://www.w3.org/1999/xhtml">
    <script/><iframe/><frame/><object/><embed/><form/><link/><meta/><base/>
    <p onclick="marker" onfocus="marker" title="good"/><a href="javascript:marker" ping="https://example.com/"/><a href="#mark"/>
    <img src="https://example.com/x"/><source src="https://example.com/x"/><img src="data:image/png;base64,aA==" srcset="https://example.com/x"/></main>`;
  const root = new DOMParser().parseFromString(markup, "application/xml").documentElement;
  const select = () => Array.from(root.getElementsByTagName("*"));
  // The sanitizer only needs these methods in the no-CSS fixture; Chromium exercises the real DOM and CSSOM.
  root.querySelectorAll = select;
  for (const el of select()) el.remove = () => el.parentNode.removeChild(el);
  cleanWord(root);
  assert.deepEqual(select().map((el) => el.nodeName), ["p", "a", "a", "img"]);
  assert.deepEqual(select().map((el) => Array.from(el.attributes, (attr) => attr.name)), [["title"], [], ["href"], ["src"]]);
});

test("Word keeps ordinary layout and local embedded resources but not CSS resource indirection", () => {
  for (const value of ["12pt", "var(--docx-color)", "rgb(1, 2, 3)", 'url("data:image/png;base64,aA==")', "counter(number)"]) assert.equal(safeWordCss(value), true, value);
  for (const value of ["url(https://example.com/a.png)", "u\\72l(https://example.com/a.png)", "image-set('https://example.com/a.png' 1x)", 'url("data:text/html;base64,aA==")', "attr(src type(<url>))"]) assert.equal(safeWordCss(value), false, value);
});
