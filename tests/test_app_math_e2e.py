"""Chat markdown's local LaTeX: `$x$`, `$$x$$`, `\\(x\\)` and `\\[x\\]` all typeset offline (KaTeX → MathML,
drawn by Qt's Chromium — no fonts, no network, no local TeX), while code, currency, link targets and broken
input stay literal. Single-line `$$x$$` and `\\[x\\]` are display math; `\\(x\\)` stays inline.

The scripted reply mirrors the reported screenshot: a numbered list whose second item holds a multi-line
$$…\\begin{cases}…\\end{cases}…$$ block (three-space indent, so it belongs to the item). (Offscreen Qt, the
loopback fake model.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui

SAMPLE = r"""1. 对任意 \(s \in \mathbb{R}\)，斜率记作 \(f'(s)\)。
2. 分段函数：
   $$
   f(x) = \begin{cases}
   x^2, & x \geq 0 \\
   -x, & x < 0
   \end{cases}
   $$
3. 行内 $2+2$、$42$ 与 $\frac{a}{b}$；金额 $5、$10 和 $20 USD 是价格。
   \[ e = mc^2 \]

$$e^{i\pi} + 1 = 0$$

\[ \int_0^1 x^2 \, dx = \frac{1}{3} \]

行中 $$m+n$$ 按整行样式排版。代码 `\(x\)`、[链接目标](http://u/?p=$5) 与围栏保持原样：

```tex
\(raw\) $literal$
```

未闭合 \( 与无效 $\oops$ 和 $\frac{1$ 保持可读。"""


async def math_summary(rig):
    raw = await rig.js("""JSON.stringify((() => {
        return {
            total: document.querySelectorAll('.math-formula').length,
            mathTags: document.querySelectorAll('.math-formula math').length,
            display: document.querySelectorAll('.math-formula.display').length,
            liDisplay: document.querySelectorAll('ol li .math-formula.display').length,
            liMath: document.querySelectorAll('ol li .math-formula math').length,
            liMtable: document.querySelectorAll('ol li .math-formula mtable').length,
            mtable: document.querySelectorAll('.math-formula mtable').length,
            mfrac: document.querySelectorAll('.math-formula mfrac').length,
            errors: document.querySelectorAll('.katex-error, .math-source').length,
            listNumbering: document.querySelectorAll('.text-block ol > li').length,
        };
    })())""")
    return json.loads(raw)


def test_all_four_delimiters_typeset_while_code_currency_and_broken_stay_literal(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1450, 980))
        try:
            model = await new_conversation(rig)
            model.next(say(SAMPLE), hold=False)
            await rig.send("把这些公式排出来")
            await rig.wait_for(".math-formula.display", 4)
            await rig.wait_for("ol li .math-formula.display mtable")

            summary = await math_summary(rig)
            assert summary["listNumbering"] == 3, "the numbered list still renders as a list"
            assert summary["liDisplay"] == 2, "the cases block and \\[e=mc^2\\] are display math inside items"
            assert summary["liMtable"] >= 1, "\\begin{cases} typeset as a table inside the list item"
            assert summary["liMath"] == 7, "\\(s\\), \\(f'(s)\\), cases, $2+2$, $42$, $\\frac{a}{b}$, \\[e=mc^2\\] in items"
            assert summary["display"] == 4, "cases + item \\[…\\] + single-line $$…$$ + single-line \\[…\\] display"
            assert summary["mtable"] >= 1 and summary["mfrac"] >= 2
            assert summary["mathTags"] == summary["total"] - summary["errors"], (
                "every formula but the broken one is real MathML"
            )
            assert summary["errors"] == 1, "$\\frac{1$ shows its source instead of breaking the text"

            body = await rig.text(".block.text-block")
            assert "金额 $5、$10 和 $20 USD" in body, "currency dollars stayed literal"
            assert "未闭合 \\(" in body, "an unclosed \\( reads as its source, not as math"
            assert "\\oops" in body, "an unknown command falls back to its source in place"
            assert "\\frac{1" in await rig.js("document.querySelector('.katex-error')?.textContent ?? ''"), (
                "an invalid formula shows what was written"
            )

            code_texts = await rig.texts(".block.text-block code")
            assert "\\(x\\)" in code_texts, "a code span never parses math"
            assert "\\(raw\\) $literal$" in code_texts, "a fenced block never parses math"

            link = await rig.js("document.querySelector('.text-block a')?.getAttribute('href') ?? ''")
            assert link.endswith("$5"), "the dollar inside the link destination was not rewritten"

            # The layout really painted before the crop (MathML has no async font or script work).
            await wait_for(
                lambda: rig.js(
                    "document.querySelector('ol li .math-formula.display mtable')?.getBoundingClientRect().height > 10"
                ),
                5,
                "the cases table laid out inside the list item",
            )
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.js("document.querySelector('.math-formula.display').scrollIntoView({block: 'center'})")
                await rig.shot(".math-formula.display", Path(evidence) / "math-chat.png")
        finally:
            await rig.finish()

    run(loop, scenario())
