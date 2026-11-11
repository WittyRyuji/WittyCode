import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { AppIcon, DragonMark, LogoVertical, Wordmark } from "./Brand";

const NEUTRALS = ["--ink", "--paper", "--bg", "--bg-side", "--bg-bar", "--bg-raised", "--soft", "--line", "--line-strong", "--fg", "--fg-2", "--muted", "--faint"];
const MEANING = ["--st-ready", "--st-running", "--st-attention", "--st-dead", "--st-opening", "--ok", "--warn", "--err"];
const DIFFS = ["--add-bg", "--del-bg", "--code-bg", "--warn-bg", "--err-bg"];

const TYPE = [
  { name: "对话正文", token: "--fs-body", size: "15px / 1.72", font: "sans", sample: "整个界面都用 React，Python 只做后端。Rendering 10x slower, 先看第 1、2 项。" },
  { name: "界面文字", token: "--fs-ui", size: "13.5px / 1.5", font: "sans", sample: "侧边栏、标签、输入框控件" },
  { name: "辅助文字", token: "--fs-small", size: "12px", font: "sans", sample: "已打开 · 刚刚　用时 12 秒 · 4 步" },
  { name: "代码 / 路径 / 数字", token: "--fs-code", size: "13px / 1.5", font: "mono", sample: "Context 10K/999K   Token In 0.4K Out 4K   C:\\Users\\me\\code" },
];

function Swatch({ name }: { name: string }) {
  const [value, setValue] = useState("");
  useEffect(() => {
    const read = () => setValue(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
    read();
    const obs = new MutationObserver(read);
    obs.observe(document.documentElement, { attributes: true });
    return () => obs.disconnect();
  }, [name]);
  return (
    <div className="sw">
      <span className="sw-chip" style={{ background: `var(${name})` }} />
      <span className="sw-name">{name}</span>
      <span className="sw-val">{value}</span>
    </div>
  );
}

export function StyleGuide({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="guide" role="dialog" aria-label="设计规范">
        <header>
          <h2>设计规范</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </header>
        <div className="guide-body selectable">
          <section>
            <h3>品牌</h3>
            <p>
              界面从 WITTY LOONG 的两个 Logo 推出来：<b>纯黑白</b>、<b>粗线条</b>、<b>直角</b>、<b>方框</b>。没有强调色——「强调」就是墨色本身（深色主题里是白，浅色主题里是黑），
              选中项、主按钮、当前标签都用反白表示，和 Logo 里反白的字牌一样。浅色主题是 Logo 的反色。
            </p>
            <div className="brand-row">
              <div className="brand-cell">
                <AppIcon size={64} />
                <span>软件图标（方形 Logo）</span>
              </div>
              <div className="brand-cell">
                <LogoVertical width={64} />
                <span>竖版 Logo：欢迎页、关于</span>
              </div>
              <div className="brand-cell">
                <DragonMark size={64} />
                <span>龙头，随文字颜色</span>
              </div>
              <div className="brand-cell">
                <Wordmark height={44} />
                <span>字标</span>
              </div>
            </div>
          </section>
          <section>
            <h3>字体</h3>
            <p>
              界面和对话用 <b>Inter</b>（随程序打包，只含拉丁字母）；中文用系统自带的 <b>微软雅黑 UI</b>；代码、路径和状态栏里的数字用 <b>JetBrains Mono</b>（随程序打包）。
              字体文件是同一份，正式界面和这页的文字排版一致。
            </p>
            <div className="type-list">
              {TYPE.map((t) => (
                <div key={t.name} className="type-row">
                  <div className="type-meta">
                    <b>{t.name}</b>
                    <span>{t.size}</span>
                  </div>
                  <div style={{ font: `var(${t.token}) / ${t.token === "--fs-body" ? "var(--lh-body)" : "1.5"} var(--font-${t.font})` }}>{t.sample}</div>
                </div>
              ))}
            </div>
          </section>
          <section>
            <h3>颜色</h3>
            <p>
              墨与纸（黑白），加上<b>只用来表达含义</b>的几种颜色：会话状态（绿 = 就绪，琥珀 = 运行中，红 = 断开，墨色光圈 = 等你确认）、成功 / 警告 / 错误、diff 的增删。
            </p>
            <div className="sw-grid">{NEUTRALS.map((n) => <Swatch key={n} name={n} />)}</div>
            <div className="sw-grid">{MEANING.map((n) => <Swatch key={n} name={n} />)}</div>
            <div className="sw-grid">{DIFFS.map((n) => <Swatch key={n} name={n} />)}</div>
          </section>
          <section>
            <h3>线条和形状</h3>
            <p>
              圆角只有 2–4px。重要的框（输入框聚焦、批准卡片、对话框、菜单）用 2px 墨色线，加一层错位的「硬阴影」，像 Logo 的双层边框；次要的分隔用 1px 淡线。
            </p>
            <div className="shapes">
              <div className="shape hard">2px 墨色线 + 硬阴影</div>
              <div className="shape thin">1px 淡线</div>
              <div className="shape inv">反白：选中 / 主按钮</div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
