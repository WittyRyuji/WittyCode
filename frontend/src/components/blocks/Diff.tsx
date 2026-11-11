import type { FileDiff } from "../../types";

const KIND_LABEL = { new: "新文件", edit: "修改", delete: "删除" } as const;

/** The directory shrinks (with an ellipsis) when the row is narrow; the file name never does. */
function PathLabel({ path }: { path: string }) {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const dir = cut >= 0 ? path.slice(0, cut + 1) : "";
  const name = cut >= 0 ? path.slice(cut + 1) : path;
  return (
    <span className="dpath" title={path}>
      {dir && <span className="ddir">{dir}</span>}
      <span className="dname">{name}</span>
    </span>
  );
}

export function Diff({ diff }: { diff: FileDiff }) {
  return (
    <div className="diff">
      <div className="dhead">
        <PathLabel path={diff.path} />
        <span className="dtag">{KIND_LABEL[diff.kind]}</span>
        <span className="dstat num">
          <span className="plus">+{diff.added}</span> <span className="minus">−{diff.removed}</span>
        </span>
      </div>
      <div className="dscroll selectable">
        <table className="dtable">
          <tbody>
            {diff.lines.map((l, i) =>
              l.t === "gap" ? (
                <tr key={i} className="d-gap">
                  <td colSpan={4}>{l.text}</td>
                </tr>
              ) : (
                <tr key={i} className={`d-${l.t}`}>
                  <td className="ln">{l.old ?? ""}</td>
                  <td className="ln">{l.new ?? ""}</td>
                  <td className="sg">{l.t === "add" ? "+" : l.t === "del" ? "−" : ""}</td>
                  <td className="tx">{l.text}</td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
