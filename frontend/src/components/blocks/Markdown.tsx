import { createContext, memo, useContext, useMemo, useState, type ReactElement } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { Check, Copy } from "lucide-react";
import { highlight, LANGUAGE_LABEL } from "../../lib/highlight";
import { inlineMediaTarget, localVideoLink } from "../../lib/inlineMedia";
import { normalizeMathSource } from "../../lib/markdownMath";
import { looksLikePath, localTarget } from "../../lib/pathref";
import { InlinePicture, InlineVideo } from "./InlineMedia";
import { MathFormula } from "./MathFormula";
import { PathRef } from "./PathRef";
import "../../styles/math.css";

export const CodeBlock = memo(function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const html = useMemo(() => language ? highlight(code, language) : null, [code, language]);
  const copy = () => {
    void navigator.clipboard?.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };
  return (
    <div className="codeblock">
      <div className="codehead">
        <span>{LANGUAGE_LABEL[language.toLowerCase()] ?? (language || "代码")}</span>
        <button type="button" className="copy" onClick={copy} onMouseDown={(e) => e.preventDefault()}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>
        {html !== null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : <code>{code}</code>}
      </pre>
    </div>
  );
});

const MediaSession = createContext<string | undefined>(undefined);
const plugins = [remarkGfm, remarkMath];
// Stable component types keep completed code and its selection/copy state while new text streams below it.
const components: Components = {
  pre({ children }) {
    const child = (Array.isArray(children) ? children[0] : children) as ReactElement<{ className?: string; children?: unknown }>;
    const props = child?.props ?? {};
    const code = String(props.children ?? "").replace(/\n$/, "");
    if (/\bmath-display\b/.test(props.className ?? "")) return <MathFormula tex={code} display />;
    const language = /language-([\w+-]+)/.exec(props.className ?? "")?.[1] ?? "";
    return <CodeBlock code={code} language={language} />;
  },
  table({ children }) {
    return <div className="table-wrap"><table>{children}</table></div>;
  },
  code({ className, children }) {
    // inline code only: a block's code is drawn by `pre` above, which never renders this
    const text = Array.isArray(children) ? children.join("") : String(children ?? "");
    if (/\bmath-inline\b/.test(className ?? "")) return <MathFormula tex={text} />;
    if (!className && looksLikePath(text)) return <PathRef text={text.trim()} />;
    return <code className={className}>{children}</code>;
  },
  a: function MarkdownLink({ href, children }) {
    const sessionId = useContext(MediaSession);
    const media = sessionId ? inlineMediaTarget(href) : null;
    if (sessionId && media?.kind === "video") {
      const label = typeof children === "string" && children.trim() ? children : media.path.split(/[\\/]/).pop()!;
      return <InlineVideo key={media.path} sessionId={sessionId} path={media.path} label={label} />;
    }
    if (sessionId && localVideoLink(href)) return <span className="img-link" title="正文视频只支持项目内的相对路径">{children}</span>;
    const local = localTarget(href);
    if (local) return <PathRef text={local} label={children} />;
    return <a href={href} title={href} onClick={(e) => e.preventDefault()}>{children}</a>;
  },
  img: function MarkdownImage({ src, alt }) {
    const sessionId = useContext(MediaSession);
    const media = sessionId ? inlineMediaTarget(src) : null;
    if (sessionId && media) {
      const label = alt || media.path.split(/[\\/]/).pop()!;
      const Embed = media.kind === "image" ? InlinePicture : InlineVideo;
      return <Embed key={media.path} sessionId={sessionId} path={media.path} label={label} />;
    }
    return <span className="img-link">[图片：{alt || "未命名"}]</span>;
  },
};

/** Assistant text. Raw HTML in the markdown is never rendered; only finished blocks opt in to local media. */
export const Markdown = memo(function Markdown({ source, streaming, mediaSessionId }: {
  source: string; streaming?: boolean; mediaSessionId?: string;
}) {
  const prepared = useMemo(() => normalizeMathSource(source), [source]);
  return (
    <MediaSession.Provider value={streaming ? undefined : mediaSessionId}>
      <div className={`text selectable${streaming ? " streaming" : ""}`}>
        <ReactMarkdown remarkPlugins={plugins} components={components}>{prepared}</ReactMarkdown>
      </div>
    </MediaSession.Provider>
  );
});
