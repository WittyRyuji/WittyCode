"""Office files named in a real conversation cross the real QWebChannel and render in Qt WebEngine."""
from __future__ import annotations

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_app_e2e import new_conversation
from tests.test_office_preview import write_package
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def relationships(*items):
    return f'<Relationships xmlns="{PR}">' + "".join(f'<Relationship Id="{id_}" Type="{R}/{type_}" Target="{target}"/>' for id_, type_, target in items) + "</Relationships>"


def make_documents(project):
    write_package(project / "brief.docx", {
        "_rels/.rels": relationships(("main", "officeDocument", "word/document.xml")),
        "word/document.xml": f'''<w:document xmlns:w="{W}"><w:body>
            <w:p><w:r><w:t>Office bridge heading</w:t></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Office bridge table</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            <w:sectPr><w:pgSz w:w="10200" w:h="13200"/><w:pgMar w:left="900" w:right="900"/></w:sectPr>
            </w:body></w:document>''',
    })
    write_package(project / "values.xlsx", {
        "_rels/.rels": relationships(("main", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Totals" sheetId="1" r:id="s1"/><sheet name="Detail" sheetId="2" r:id="s2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": relationships(("s1", "worksheet", "worksheets/sheet1.xml"), ("s2", "worksheet", "worksheets/sheet2.xml")),
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Total</t></is></c><c r="B1"><v>42</v></c></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Bridge detail</t></is></c></row></sheetData></worksheet>',
    })
    write_package(project / "summary.pptx", {
        "_rels/.rels": relationships(("main", "officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="slide"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": relationships(("slide", "slide", "slides/slide1.xml")),
        "ppt/slides/slide1.xml": f'''<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>
            <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
            <p:txBody><a:p><a:r><a:t>Office bridge slide</a:t></a:r></a:p></p:txBody>
            </p:sp></p:spTree></p:cSld></p:sld>''',
    })


def test_office_files_open_in_the_real_window(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            make_documents(rig.project)
            model = await new_conversation(rig)
            model.next(say("Examples: `brief.docx`, `values.xlsx`, and `summary.pptx`."), hold=False)
            await rig.send("show the examples")
            await rig.wait_for(".text-block code.pathref", 3)

            await rig.click(".text-block code.pathref", 0)
            await rig.wait_for(".fp-word-paper", 1)
            assert await rig.js("document.querySelector('.fp-word-paper').shadowRoot.querySelector('p').textContent") == "Office bridge heading"
            assert await rig.js("document.querySelector('.fp-word-paper').shadowRoot.querySelector('td').textContent") == "Office bridge table"
            assert "Word 文档" in await rig.text(".fp-facts")

            await rig.click(".text-block code.pathref", 1)
            await rig.wait_for(".fp-grid td", 2)
            assert await rig.texts(".fp-grid td") == ["Total", "42"]
            await rig.click(".fp-tabs button", 1)
            await rig.wait_text(".fp-grid td", "Bridge detail")

            await rig.click(".text-block code.pathref", 2)
            await rig.wait_text(".fp-slide h3", "Office bridge slide")
            assert "1 张幻灯片" in await rig.text(".fp-facts")
            assert not await rig.exists(".fp-grid, .fp-word-paper, .fp-error")
        finally:
            await rig.finish()

    run(loop, scenario())
