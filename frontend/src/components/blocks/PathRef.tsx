import type { ReactNode } from "react";
import { useStore } from "../../store";

/** A file the model named, in its text. A click shows it in the side panel; Ctrl+click opens it with the system's default
 *  program; right click offers the other ways (choose the program, show in its folder, copy the path). */
export function PathRef({ text, label }: { text: string; label?: ReactNode }) {
  const openFile = useStore((s) => s.openFile);
  const showMenu = useStore((s) => s.showPathMenu);
  const go = (e: { ctrlKey: boolean; metaKey: boolean; preventDefault: () => void }) => {
    e.preventDefault();
    openFile(text, e.ctrlKey || e.metaKey ? "default" : "panel");
  };
  const Tag = label === undefined ? "code" : "span"; // (a link with words of its own reads as a link, a bare path as code)
  return (
    <Tag
      className="pathref"
      role="link"
      tabIndex={0}
      title="点击在右侧打开 · Ctrl+点击用默认程序打开 · 右键选打开方式"
      onClick={go}
      onKeyDown={(e) => e.key === "Enter" && go(e)}
      onContextMenu={(e) => {
        e.preventDefault();
        showMenu(text, e.clientX, e.clientY);
      }}
    >
      {label ?? text}
    </Tag>
  );
}
