import JSZip from "jszip";
import { utils, write } from "xlsx";

const W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const P = "http://schemas.openxmlformats.org/presentationml/2006/main";
const A = "http://schemas.openxmlformats.org/drawingml/2006/main";
const PR = "http://schemas.openxmlformats.org/package/2006/relationships";
export const PROBE = "https://office-preview.invalid";
const MARK = "window.officePreviewMarker=1";
const rel = (id, type, target, external = false) => `<Relationship Id="${id}" Type="${R}/${type}" Target="${target}"${external ? ' TargetMode="External"' : ""}/>`;
const rels = (...entries) => `<Relationships xmlns="${PR}">${entries.join("")}</Relationships>`;
const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY2sAAAAASUVORK5CYII=";
const xml = (text) => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll("\"", "&quot;");
export const LITERAL = "<img src=x onerror=marker>";

export async function wordFixture(hostile = false) {
  const zip = new JSZip();
  zip.file("[Content_Types].xml", `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>`);
  zip.file("_rels/.rels", rels(rel("r1", "officeDocument", "word/document.xml")));
  zip.file("word/_rels/document.xml.rels", rels(rel("s", "styles", "styles.xml"), rel("good", "hyperlink", "https://example.com/office-demo", true), rel("bad", "hyperlink", `javascript:${MARK}`, true), rel("ext", "image", `${PROBE}/word.png`, true), rel("chunk", "aFChunk", "chunk.html"), rel("pic", "image", "media/sample.png")));
  zip.file("word/chunk.html", `<div>ALT CHUNK MUST NOT RENDER</div><script>${MARK}</script><img src="${PROBE}/alt.png">`);
  zip.file("word/media/sample.png", png, { base64: true });
  const p = (text, props = "") => `<w:p>${props}<w:r><w:t>${xml(text)}</w:t></w:r></w:p>`;
  const link = (id, text) => `<w:p><w:hyperlink r:id="${id}"><w:r><w:t>${text}</w:t></w:r></w:hyperlink></w:p>`;
  const pic = (id) => `<w:p><w:r><w:drawing><wp:inline><wp:extent cx="381000" cy="381000"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="${id}"/></pic:blipFill><pic:spPr><a:xfrm><a:ext cx="381000" cy="381000"/></a:xfrm></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>`;
  zip.file("word/styles.xml", `<w:styles xmlns:w="${W}"><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="56"/><w:color w:val="24477C"/></w:rPr></w:style></w:styles>`);
  zip.file("word/document.xml", `<w:document xmlns:w="${W}" xmlns:r="${R}" xmlns:a="${A}" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" xmlns:v="urn:schemas-microsoft-com:vml"><w:body>
    ${p("项目简报", '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>')}${p("本周完成了样例数据核对。")}
    <w:tbl><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="8"/><w:bottom w:val="single" w:sz="8"/></w:tblBorders></w:tblPr><w:tr><w:tc>${p("事项")}</w:tc><w:tc>${p("进度")}</w:tc></w:tr><w:tr><w:tc>${p("样例数据")}</w:tc><w:tc>${p("已完成")}</w:tc></w:tr></w:tbl>
    ${hostile ? `${link("bad", "Blocked link")}${link("good", "External link")}${pic("ext")}<w:altChunk r:id="chunk"/><w:p><w:r><w:pict><v:rect style="background-image:url(${PROBE}/word-style.png)" fillcolor="url(${PROBE}/word-fill.png)"/></w:pict></w:r></w:p>` : pic("pic")}
    <w:sectPr><w:pgSz w:w="10200" w:h="13200"/><w:pgMar w:top="900" w:bottom="900" w:left="900" w:right="900"/></w:sectPr>
  </w:body></w:document>`);
  return zip.generateAsync({ type: "uint8array", compression: "DEFLATE" });
}

export function sheetFixture(hostile = false) {
  const book = utils.book_new();
  const sheet = utils.aoa_to_sheet([["月份", "数量", "完成率"], ["一月", 12, 0.75], ["二月", 18, 0.90], ["说明", "样例数据"], ...(hostile ? [[LITERAL, "=1+1", "external", 5]] : [])]);
  sheet.C2.z = "0.0%";
  sheet.C3.z = "0.0%";
  sheet["!merges"] = [utils.decode_range("B4:C4")];
  if (hostile) {
    sheet.C5.l = { Target: `${PROBE}/workbook` };
    sheet.D5.f = `WEBSERVICE("${PROBE}/formula")`;
  }
  utils.book_append_sheet(book, sheet, "月度汇总");
  utils.book_append_sheet(book, utils.aoa_to_sheet([["项目", "状态"], ["校对", "已完成"]]), "明细");
  utils.book_append_sheet(book, utils.aoa_to_sheet([]), "空表");
  return new Uint8Array(write(book, { type: "array", bookType: "xlsx" }));
}

export async function slidesFixture(hostile = false) {
  const zip = new JSZip();
  const text = (s, props = "") => `<a:p>${props}<a:r><a:t>${xml(s)}</a:t></a:r></a:p>`;
  const title = (s) => `<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody>${text(s)}</p:txBody></p:sp>`;
  const body = (...ps) => `<p:sp><p:txBody>${ps.join("")}</p:txBody></p:sp>`;
  const picture = (id) => `<p:pic><p:nvPicPr><p:cNvPr descr="样例标记"/></p:nvPicPr><p:blipFill><a:blip r:embed="${id}"/></p:blipFill></p:pic>`;
  const table = (...rows) => `<p:graphicFrame><a:graphic><a:graphicData><a:tbl>${rows.map((row) => `<a:tr>${row.map((s) => `<a:tc><a:txBody>${text(s)}</a:txBody></a:tc>`).join("")}</a:tr>`).join("")}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>`;
  const slide = (...shapes) => `<p:sld xmlns:p="${P}" xmlns:a="${A}" xmlns:r="${R}"><p:cSld><p:spTree>${shapes.join("")}</p:spTree></p:cSld></p:sld>`;
  zip.file("[Content_Types].xml", `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>`);
  zip.file("_rels/.rels", rels(rel("r1", "officeDocument", "ppt/presentation.xml")));
  zip.file("ppt/presentation.xml", `<p:presentation xmlns:p="${P}" xmlns:r="${R}"><p:sldIdLst><p:sldId id="256" r:id="later"/><p:sldId id="257" r:id="earlier"/></p:sldIdLst></p:presentation>`);
  zip.file("ppt/_rels/presentation.xml.rels", rels(rel("earlier", "slide", "slides/slide1.xml"), rel("later", "slide", "slides/slide2.xml")));
  zip.file("ppt/slides/slide1.xml", slide(title("下一步"), body(text("更新样例，继续跟踪。"))));
  zip.file("ppt/slides/slide2.xml", slide(title("本周进度"), body(text("已核对样例数据。", '<a:pPr><a:buChar char="•"/></a:pPr>'), text("检查了两个月份。", '<a:pPr lvl="1"/>'), ...(hostile ? [text(LITERAL), text("=1+1")] : [])),
    table(["事项", "状态"], ["样例数据", "已完成"]), picture("img"), ...(hostile ? [picture("ext"), picture("scheme")] : [])));
  zip.file("ppt/slides/_rels/slide2.xml.rels", rels(rel("img", "image", "../media/sample.png"), rel("ext", "image", `${PROBE}/slide.png`, true), rel("scheme", "image", `${PROBE}/absolute.png`)));
  zip.file("ppt/media/sample.png", png, { base64: true });
  return zip.generateAsync({ type: "uint8array", compression: "DEFLATE" });
}

export async function officeFixtures() {
  const specs = [
    ["docx", "word", "wordprocessingml.document", wordFixture], ["xlsx", "sheet", "spreadsheetml.sheet", sheetFixture], ["pptx", "slides", "presentationml.presentation", slidesFixture],
  ];
  const files = {};
  for (const [extension, kind, mime, fixture] of specs) {
    for (const [prefix, hostile] of [["sample", false], ["hostile", true]]) {
      const bytes = await fixture(hostile);
      files[`${prefix}.${extension}`] = { kind, size: bytes.length, url: `data:application/vnd.openxmlformats-officedocument.${mime};base64,${Buffer.from(bytes).toString("base64")}` };
    }
  }
  return files;
}
