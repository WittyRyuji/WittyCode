import { Copy, MoreHorizontal, Minus, Square, X } from "lucide-react";
import { useStore } from "../store";
import { bridge } from "../bridge-instance";
import { messageOf } from "../lib/errors";
import { AppIcon } from "./Brand";
import { MenuButton, type MenuItem } from "./ui";

export function useAppMenu(): MenuItem[] {
  const s = useStore();
  const cur = s.currentId ? s.sessions[s.currentId] : null;
  const running = cur?.mark === "running" || cur?.mark === "attention";
  return [
    { id: "new", label: "新会话", shortcut: "Ctrl+N", onSelect: () => s.newSession() },
    { id: "add", label: "添加项目文件夹…", shortcut: "Ctrl+O", onSelect: s.addProject },
    { id: "close", label: "关闭当前会话", shortcut: "Ctrl+W", disabled: !cur, onSelect: () => cur && s.closeTab(cur.id) },
    { id: "sep1", separator: true },
    { id: "stop", label: "停止当前任务", shortcut: "Esc", disabled: !running, onSelect: () => cur && s.stop(cur.id) },
    { id: "reconnect", label: "重新连接", disabled: cur?.mark !== "dead", onSelect: () => cur && s.reconnect(cur.id) },
    { id: "focus", label: "聚焦输入框", shortcut: "Ctrl+L", disabled: !cur, onSelect: () => window.dispatchEvent(new Event("wc:focus-composer")) },
    { id: "end", label: "跳到最新消息", shortcut: "Ctrl+End", disabled: !cur, onSelect: () => window.dispatchEvent(new Event("wc:scroll-end")) },
    { id: "refresh", label: "刷新会话列表", shortcut: "F5", onSelect: () => s.refreshProjects() },
    { id: "sep2", separator: true },
    { id: "settings", label: "设置…", shortcut: "Ctrl+,", onSelect: () => s.openSettings("appearance") },
    { id: "quit", label: "退出", shortcut: "Ctrl+Q", onSelect: s.quit },
  ];
}

/** The window's title bar, drawn by the page (the Qt window has no frame of its own): the app icon and name, the app
 *  menu, an empty stretch that drags the window, and the three window buttons. Only the parts marked `data-drag` start a
 *  move, so the menu and its popup never do. `menu` false: the live app is still connecting, there is nothing to offer. */
export function TitleBar({ menu = true }: { menu?: boolean }) {
  const appMenu = useAppMenu();
  const maximized = useStore((s) => s.maximized);
  const minimize = useStore((s) => s.minimizeWindow);
  const toggle = useStore((s) => s.toggleMaximize);
  const quit = useStore((s) => s.quit);
  const say = useStore((s) => s.say);

  const dragZone = (e: React.MouseEvent) => e.target instanceof HTMLElement && e.target.hasAttribute("data-drag");
  return (
    <header
      className="titlebar"
      data-drag
      onMouseDown={(e) => {
        if (e.button === 0 && e.detail < 2 && dragZone(e)) bridge.window.startMove().catch((error) => say(messageOf(error)));
      }}
      onDoubleClick={(e) => dragZone(e) && void toggle()}
    >
      <span className="tb-brand" data-drag>
        <AppIcon size={20} />
        <span className="tb-title">WittyCode</span>
      </span>
      {menu && (
        <MenuButton className="icon-btn sm tb-menu" title="菜单" items={appMenu} minWidth={240}>
          <MoreHorizontal size={16} />
        </MenuButton>
      )}
      <span className="tb-drag" data-drag />
      <button type="button" className="tb-btn" aria-label="最小化" title="最小化" onClick={minimize}>
        <Minus size={15} />
      </button>
      <button type="button" className="tb-btn" aria-label={maximized ? "还原" : "最大化"} title={maximized ? "还原" : "最大化"} onClick={() => void toggle()}>
        {maximized ? <Copy size={12} /> : <Square size={11} />}
      </button>
      <button type="button" className="tb-btn close" aria-label="关闭" title="关闭" onClick={quit}>
        <X size={16} />
      </button>
    </header>
  );
}
