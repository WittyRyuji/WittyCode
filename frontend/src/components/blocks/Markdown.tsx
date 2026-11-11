import { memo, useState, type ReactElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { highlight, LANGUAGE_LABEL } from "../../lib/highlight";
import { looksLikePath, localTarget } from "../../lib/pathref";
import { PathRef } from "./PathRef";

export function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const html = language ? highlight(code, language) : null;
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
}

/** Assistant text. Raw HTML in the markdown is never rendered (react-markdown drops it), links are inert text until
 *  the Python side opens them. */
export const Markdown = memo(function Markdown({ source, streaming }: { source: string; streaming?: boolean }) {
  return (
    <div className={`text selectable${streaming ? " streaming" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre({ children }) {
            const child = (Array.isArray(children) ? children[0] : children) as ReactElement<{ className?: string; children?: unknown }>;
            const props = child?.props ?? {};
            const language = /language-([\w+-]+)/.exec(props.className ?? "")?.[1] ?? "";
            return <CodeBlock code={String(props.children ?? "").replace(/\n$/, "")} language={language} />;
          },
          table({ children }) {
            return (
              <div className="table-wrap">
                <table>{children}</table>
              </div>
            );
          },
          code({ className, children }) {
            // inline code only: a block's code is drawn by `pre` above, which never renders this
            const text = Array.isArray(children) ? children.join("") : String(children ?? "");
            if (!className && looksLikePath(text)) return <PathRef text={text.trim()} />;
            return <code className={className}>{children}</code>;
          },
          a({ href, children }) {
            const local = localTarget(href);
            if (local) return <PathRef text={local} label={children} />;
            return (
              <a href={href} title={href} onClick={(e) => e.preventDefault()}>
                {children}
              </a>
            );
          },
          img({ alt }) {
            return <span className="img-link">[图片：{alt || "未命名"}]</span>;
          },
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
});
