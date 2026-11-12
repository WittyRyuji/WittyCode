import { useEffect, useState } from "react";
import { EyeOff, Palette } from "lucide-react";
import { useStore } from "../store";
import type { ThemeChoice } from "../types";
import { MenuButton, type MenuItem } from "./ui";

export const WIDTHS = [
  { id: "auto", label: "自适应", width: 0 },
  { id: "1280", label: "1280", width: 1280 },
  { id: "1024", label: "1024", width: 1024 },
  { id: "800", label: "800", width: 800 },
];

function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg" role="group">
      {options.map((o) => (
        <button key={o.id} type="button" className={value === o.id ? "on" : ""} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ReviewBar({ width, onWidth, onGuide }: { width: string; onWidth: (id: string) => void; onGuide: () => void }) {
  const s = useStore();
  const lightboxSource = s.sessions["s-review"]?.turns[0]?.blocks.find((b) => b.kind === "user");
  const [flash, setFlash] = useState(false);
  useEffect(() => {
    if (!flash) return;
    s.setDragging(true);
    const t = window.setTimeout(() => {
      s.setDragging(false);
      setFlash(false);
    }, 2600);
    return () => window.clearTimeout(t);
  }, [flash]); // eslint-disable-line react-hooks/exhaustive-deps

  const scenes: MenuItem[] = [
    { id: "none", label: "没有会话（欢迎页）", onSelect: () => s.select(null) },
    { id: "empty", label: "刚创建的空会话", onSelect: () => s.select("s-empty") },
    { id: "done", label: "已完成的对话（工具调用折叠）", onSelect: () => s.select("s-file-ops") },
    { id: "long", label: "长回答：表格、代码、附件", onSelect: () => s.select("s-review") },
    { id: "live", label: "运行中（转圈、流式文字）", onSelect: () => s.select("s-live") },
    { id: "approve", label: "等待你批准（带 diff）", onSelect: () => s.select("s-approval") },
    { id: "ask", label: "模型在提问", onSelect: () => s.select("s-question") },
    { id: "dead", label: "连接已断开", onSelect: () => s.select("s-dead") },
    { id: "index", label: "另一个 ChatGPT Plan 会话（另一个模型）", onSelect: () => s.select("s-index") },
    { id: "devin", label: "Devin 的会话（权限模式、每个模型自己的思考档位）", onSelect: () => s.select("s-devin") },
    { id: "chatgpt", label: "ChatGPT Plan 的会话（Using ChatGPT Plan、Manage usage、额度圆环）", onSelect: () => s.select("s-chatgpt") },
    { id: "api", label: "API Provider 的会话（没有推理档位）", onSelect: () => s.select("s-api") },
    { id: "switched", label: "同一对话里换到 Devin 之后（交接）", onSelect: () => { s.select("s-file-ops"); useStore.getState().choose("model", "devin:swe-2-high"); } },
    { id: "sep", separator: true },
    { id: "settings", label: "设置", onSelect: () => s.openSettings("appearance") },
    { id: "settings-providers", label: "设置：账号与模型", onSelect: () => s.openSettings("providers") },
    { id: "provider-new", label: "设置：添加 API Provider", onSelect: () => s.openSettings("providers", "new:openrouter") },
    { id: "provider-edit", label: "设置：编辑 API Provider", onSelect: () => s.openSettings("providers", "openrouter") },
    { id: "devin-out", label: "Devin 没登录时", onSelect: () => { useStore.setState({ devin: { ...s.devin, auth: "signed-out", email: undefined, plan: undefined } }); s.openSettings("providers"); } },
    { id: "chatgpt-out", label: "ChatGPT Plan 没登录时", onSelect: () => { useStore.setState({ chatgpt: { ...s.chatgpt, auth: "signed-out", email: "", planEnabled: false, accounts: [] } }); s.openSettings("providers"); } },
    { id: "chatgpt-welcome", label: "ChatGPT Plan 首次登录后的提示", onSelect: () => useStore.setState({ chatgpt: { ...s.chatgpt, welcomed: false } }) },
    { id: "settings-defaults", label: "设置：新会话默认值", onSelect: () => s.openSettings("defaults") },
    {
      id: "dialog",
      label: "确认对话框",
      onSelect: () =>
        s.openDialog({ title: "关闭正在运行的会话？", body: "「重构 session_manager 的上下文读取」还在运行。关闭会中断当前任务，已经完成的修改不会撤销。", confirmLabel: "中断并关闭", danger: true, onConfirm: () => s.say("（演示）已关闭。") }),
    },
    { id: "lightbox", label: "图片预览", onSelect: () => lightboxSource?.kind === "user" && lightboxSource.attachments?.[0] && s.openLightbox(lightboxSource.attachments[0]) },
    { id: "drop", label: "拖入文件的提示", onSelect: () => setFlash(true) },
    { id: "notice", label: "状态栏提示", onSelect: () => s.say("已自动关闭最久未使用的会话「修复点击闪烁」以腾出位置，可在侧边栏重新打开。") },
    { id: "sep2", separator: true },
    { id: "reload", label: "重置演示数据", onSelect: () => window.location.reload() },
  ];

  return (
    <div className="reviewbar">
      <span className="rb-title">设计审阅</span>
      <label className="rb-group">
        主题
        <Seg<ThemeChoice>
          value={s.themeChoice}
          onChange={s.setTheme}
          options={[
            { id: "light", label: "浅色" },
            { id: "dark", label: "深色" },
            { id: "system", label: "跟随系统" },
          ]}
        />
      </label>
      <label className="rb-group">
        窗口宽度
        <Seg value={width} onChange={onWidth} options={WIDTHS.map((w) => ({ id: w.id, label: w.label }))} />
      </label>
      <MenuButton className="rb-btn" items={scenes} minWidth={260}>
        跳到场景 ▾
      </MenuButton>
      <button type="button" className="rb-btn" onClick={onGuide}>
        <Palette size={13} /> 设计规范
      </button>
      <span className="rb-spacer" />
      <span className="rb-note">这页是真实 React 组件 + 模拟数据，可以点、可以输入</span>
      <button type="button" className="rb-btn" onClick={() => s.setReviewOpen(false)} title="隐藏审阅栏和模拟窗口外框">
        <EyeOff size={13} /> 隐藏
      </button>
    </div>
  );
}
