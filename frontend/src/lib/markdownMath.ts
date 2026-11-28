/** Chat markdown carries LaTeX in four delimiters — `$x$`, `$$x$$`, `\(x\)` and `\[x\]` — but remark-math only
 *  understands the two dollar forms, and only `$$` on its own lines becomes display math. ``normalizeMathSource``
 *  rewrites the backslash forms to dollar forms and escapes dollars that are currency or plain text, so the parser
 *  sees only real math.
 *
 *  The scan is context-aware: fenced code blocks (also inside blockquotes and shallow list items), indented code,
 *  inline code spans and link destinations are copied untouched — a `\(` or `$` inside them is text, not math.
 *  `\\` pairs are honoured, and a delimiter is only rewritten while still inside one paragraph (a blank line or
 *  a fence line ends the search). Anything unmatched stays literal, which is also what a half-typed formula looks
 *  like while the model is still streaming. Math content itself is opaque: a fence-looking line inside `$$…$$`
 *  is a known edge the scan does not special-case.
 *
 *  Display rules: `$$x$$` and `\[x\]` standing alone on their line become `$$\nx\n$$` blocks; mid-line they keep
 *  their spot but get `\displaystyle`, since a real display block cannot live inside a paragraph.
 *
 *  Single dollars pair by Pandoc's rules, tightened for chat prices: the opener is followed by a non-space char;
 *  when that char is a digit the pair must close before any whitespace, so `$2+2$` and `$42$` are math while
 *  `$5` and `$20 USD` are prices. The closer is not preceded by whitespace and not followed by a digit — so
 *  `$5 and $10` is text while `$x$` is math. Dollars that never close get a `\` in front and read as plain text.
 */

const IFENCE = /^ {0,6}(`{3,}|~{3,})/; // a fence at the top level or indented inside a shallow list item
const ICLOSE = /^ {0,6}(`{3,}|~{3,})[ \t\r]*$/;
const QPFX = /^ {0,3}(?:> ?){1,3}/; // a line still inside a blockquote carries this prefix
const QFENCE = /^ {0,3}(?:> ?){1,3} {0,3}(`{3,}|~{3,})/; // a fence opening inside a blockquote
const QCLOSE = /^ {0,3}(?:> ?){1,3} {0,3}(`{3,}|~{3,})[ \t\r]*$/;
const PFENCE = /^ {0,6}(?:> ?){0,3} {0,6}(`{3,}|~{3,})/; // any fence-looking line ends a paragraph
const INDENTED = /^(?: {4}|\t)/;
const BLANK = /^[ \t\r]*$/;
const PUNCT = /[!-/:-@[-`{-~]/;
const SPACE = /[ \t\n\r]/;
const DIGIT = /\d/;
const AUTOLINK = /^<[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^<>\s]*>/;
const MAX_SPAN = 2000; // a pair further apart than the per-formula size cap gives up as unclosed

type Scan = { found: number } | { skip: number } | { stop: true } | null;

/** First index ``accept`` finds at or after ``from`` while both stay inside the same paragraph: a blank line or a
 *  fence line ends the search (inline constructs cannot cross them). ``accept`` answers ``{found}`` to hit,
 *  ``{skip}`` to jump ahead, ``{stop}`` to give up early, ``null`` to step one char. -1 when nothing is found. */
function findInParagraph(source: string, from: number, accept: (index: number) => Scan): number {
  const limit = Math.min(source.length, from + MAX_SPAN); // no opener may look further: unclosed \( spam stays linear
  for (let i = from; i < limit;) {
    if (source[i] === "\n") {
      let j = i + 1;
      while (source[j] === " " || source[j] === "\t" || source[j] === "\r") j++;
      if (j >= limit || source[j] === "\n") return -1;
      const end = source.indexOf("\n", j);
      if (PFENCE.test(source.slice(j, end === -1 ? source.length : end))) return -1;
    }
    const scan = accept(i);
    if (scan === null) {
      i += 1;
    } else if ("stop" in scan) {
      return -1;
    } else if ("skip" in scan) {
      i = scan.skip;
    } else {
      return scan.found;
    }
  }
  return -1;
}

/** How many backslashes end right before ``index``: odd means the char there is escaped. */
function backslashesBefore(source: string, index: number): number {
  let count = 0;
  for (let j = index - 1; j >= 0 && source[j] === "\\"; j--) count++;
  return count;
}

/** Index just past the ``\\)`` or ``\\]`` closing a ``\\(`` / ``\\[`` opened at ``from - 2``, or -1 when the
 *  pair never closes inside the paragraph. `\\` pairs inside are skipped so `a\\\\)b` cannot fake the closer. */
function backslashPairEnd(source: string, from: number, close: string): number {
  const end = findInParagraph(source, from, (i) => {
    if (source.startsWith("\\\\", i)) return { skip: i + 2 };
    if (source.startsWith(close, i)) return { found: i };
    return null;
  });
  return end === -1 ? -1 : end + 2;
}

/** Index just past an inline code span opened by ``ticks`` (a run of that many backticks), or -1 when the span never
 *  closes inside the paragraph. */
function codeSpanEnd(source: string, from: number, ticks: number): number {
  const end = findInParagraph(source, from, (i) => {
    if (source[i] !== "`") return null;
    let j = i;
    while (source[j] === "`") j++;
    return j - i === ticks ? { found: i } : { skip: j }; // a run of a different length is content
  });
  return end === -1 ? -1 : end + ticks;
}

/** Index just past the `)` ending a `](` link destination at ``from - 2``: parens balance and `\)` never counts.
 *  -1 when the destination never closes inside the paragraph. */
function linkDestEnd(source: string, from: number): number {
  let depth = 0;
  const end = findInParagraph(source, from, (i) => {
    if (source[i] === "\\") return { skip: i + 2 };
    if (source[i] === "(") depth++;
    else if (source[i] === ")") {
      if (depth === 0) return { found: i };
      depth--;
    }
    return null;
  });
  return end === -1 ? -1 : end + 1;
}

/** Index of the lone ``$`` closing inline math opened at ``openIndex``, by the pairing rules above; -1 when none.
 *  ``tight`` (a digit opener) ends the search at the first whitespace: `$2+2$` pairs, `$5 and` never does. */
function dollarClose(source: string, openIndex: number, tight: boolean): number {
  return findInParagraph(source, openIndex + 1, (i) => {
    if (tight && SPACE.test(source[i])) return { stop: true };
    if (source[i] !== "$") return null;
    let run = 1;
    while (source[i + run] === "$") run++;
    if (run > 1) return { skip: i + run }; // a $$ cannot close a single $: it is data inside
    if (backslashesBefore(source, i) % 2 === 1) return { skip: i + 1 }; // `\$` is text
    if (SPACE.test(source[i - 1]) || DIGIT.test(source[i + 1] ?? "")) return { skip: i + 1 };
    return { found: i };
  });
}

/** Index of the ``$$`` closing a ``$$`` opened at ``openIndex``, or -1. Only an exact two-dollar run closes. */
function ddollarClose(source: string, openIndex: number): number {
  return findInParagraph(source, openIndex + 2, (i) => {
    if (source[i] !== "$") return null;
    let run = 1;
    while (source[i + run] === "$") run++;
    return run === 2 ? { found: i } : { skip: i + run };
  });
}

/** ``$`` inside math content would break the pair early or error KaTeX: every dollar not already escaped gets a
 *  backslash (KaTeX renders `\$` as a literal dollar). */
function escapeDollars(text: string): string {
  let out = "";
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "$" && backslashesBefore(text, i) % 2 === 0) out += "\\";
    out += text[i];
  }
  return out;
}

/** When the pair opened at ``i`` has only whitespace back to its line start and from ``after`` to the line end, that
 *  whitespace (the line's indent, e.g. a list item's `   `) is returned: the emitted `$$` block repeats it on every
 *  new line so the display math stays inside the same container. ``null`` when the pair is mid-line. */
function ownLine(source: string, i: number, after: number): string | null {
  const lineStart = source.lastIndexOf("\n", i - 1) + 1;
  const indent = source.slice(lineStart, i);
  if (!BLANK.test(indent)) return null;
  const eol = source.indexOf("\n", after);
  if (!BLANK.test(source.slice(after, eol === -1 ? source.length : eol))) return null;
  return indent;
}

export function normalizeMathSource(source: string): string {
  if (!source.includes("$") && !source.includes("\\")) return source;
  const n = source.length;
  const out: string[] = [];
  let fence: { mark: string; len: number; quoted: boolean } | null = null;
  let prevBlank = true; // the document head counts as blank: indented code may start at once
  let prevIndented = false; // the previous line was inside an indented code block
  let i = 0;

  while (i < n) {
    if (i === 0 || source[i - 1] === "\n") {
      const eol = source.indexOf("\n", i);
      const end = eol === -1 ? n : eol;
      const lineEnd = eol === -1 ? n : eol + 1;
      const line = source.slice(i, end);
      if (fence) {
        if (fence.quoted && !QPFX.test(line)) {
          fence = null; // a line without `>` leaves the quoted fence: process it normally below
        } else {
          out.push(source.slice(i, lineEnd));
          const close = (fence.quoted ? QCLOSE : ICLOSE).exec(line);
          if (close && close[1][0] === fence.mark && close[1].length >= fence.len) fence = null;
          i = lineEnd;
          continue;
        }
      }
      const qm = QFENCE.exec(line);
      const im = qm ? null : IFENCE.exec(line);
      const open = qm ?? im;
      if (open) {
        out.push(source.slice(i, lineEnd));
        fence = { mark: open[1][0], len: open[1].length, quoted: qm !== null };
        prevBlank = false;
        prevIndented = false;
        i = lineEnd;
        continue;
      }
      if (BLANK.test(line)) {
        out.push(source.slice(i, lineEnd));
        prevBlank = true; // a blank line inside indented code keeps the block open
        i = lineEnd;
        continue;
      }
      if (INDENTED.test(line) && (prevBlank || prevIndented)) {
        out.push(source.slice(i, lineEnd)); // indented code: delimiters are text
        prevIndented = true;
        prevBlank = false;
        i = lineEnd;
        continue;
      }
      prevBlank = false;
      prevIndented = false;
    }

    const ch = source[i];
    if (ch === "`") {
      let ticks = 1;
      while (source[i + ticks] === "`") ticks++;
      const end = codeSpanEnd(source, i + ticks, ticks);
      if (end === -1) {
        out.push(source.slice(i, i + ticks)); // never closes: the run is literal text
        i += ticks;
      } else {
        out.push(source.slice(i, end)); // a code span's content is never math
        i = end;
      }
      continue;
    }
    if (ch === "\\") {
      const next = source[i + 1] ?? "";
      if (next === "(" || next === "[") {
        const end = backslashPairEnd(source, i + 2, next === "(" ? "\\)" : "\\]");
        if (end === -1) {
          out.push("\\\\", next); // never closes: escape the backslash so `\(` / `\[` reads as written
          i += 2;
        } else if (next === "(") {
          out.push("$", escapeDollars(source.slice(i + 2, end - 2)), "$");
          i = end;
        } else {
          const content = escapeDollars(source.slice(i + 2, end - 2));
          const indent = ownLine(source, i, end);
          if (indent !== null) {
            // a standalone \[…\] is display math; the indent keeps the block inside its list item
            out.push("$$\n", indent, content.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, ""), "\n", indent, "$$");
          } else {
            out.push("$$\\displaystyle ", content, "$$");
          }
          i = end;
        }
      } else if (PUNCT.test(next)) {
        out.push(ch, next); // a backslash escape (`\\`, `\$`, …) is not math
        i += 2;
      } else {
        out.push(ch);
        i += 1;
      }
      continue;
    }
    if (ch === "$") {
      let run = 1;
      while (source[i + run] === "$") run++;
      if (run === 2 && source[i + 2] !== undefined && source[i + 2] !== "\n" && source[i + 2] !== "\r") {
        const close = ddollarClose(source, i);
        if (close !== -1) {
          const content = escapeDollars(source.slice(i + 2, close));
          const indent = ownLine(source, i, close + 2);
          if (indent !== null) {
            // a standalone $$…$$ is display math; the indent keeps the block inside its list item
            out.push("$$\n", indent, content.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, ""), "\n", indent, "$$");
          } else {
            out.push("$$\\displaystyle ", content, "$$");
          }
          i = close + 2;
          continue;
        }
      }
      if (run > 1) {
        out.push(source.slice(i, i + run)); // other $$ sequences belong to remark-math, flow or inline
        i += run;
        continue;
      }
      const next = source[i + 1] ?? "";
      const close = next !== "" && !SPACE.test(next) ? dollarClose(source, i, DIGIT.test(next)) : -1;
      if (close === -1) {
        out.push("\\$"); // currency or a stray dollar: render it literally
        i += 1;
      } else {
        out.push("$", escapeDollars(source.slice(i + 1, close)), "$");
        i = close + 1;
      }
      continue;
    }
    if (ch === "]") {
      const next = source[i + 1];
      const end = next === "("
        ? linkDestEnd(source, i + 2)
        : next === "["
          ? findInParagraph(source, i + 2, (j) => (source[j] === "]" ? { found: j } : null)) + 1
          : -1;
      if (end > 0) {
        out.push(source.slice(i, end)); // a link target is never math
        i = end;
        continue;
      }
    }
    if (ch === "<") {
      const link = AUTOLINK.exec(source.slice(i, Math.min(i + 300, n)));
      if (link) {
        out.push(link[0]); // an autolink's text is the URL itself: never math
        i += link[0].length;
        continue;
      }
    }
    out.push(ch);
    i += 1;
  }
  return out.join("");
}
