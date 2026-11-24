import { useEffect, useState } from "react";
import { version } from "../../package.json";
import { Brain, Brush, Cpu, ExternalLink, FolderOpen, HardDrive, Info, KeyRound, Layers, Minus, Plus, SlidersHorizontal, Sparkles, Trash2, X } from "lucide-react";
import { useStore } from "../store";
import { aboutContact, PROJECT_GITHUB_URL } from "../lib/about";
import { MODES, effortLabel, permissionsFor, reasoningChoices } from "../lib/vocab";
import { engineOfSource, findModel } from "../lib/models";
import { LIVE } from "../mode";
import type { FontScale, ThemeChoice } from "../types";
import type { FollowUp } from "../protocol";
import { LogoVertical } from "./Brand";
import { COMPACT_AT_MAX, COMPACT_AT_MIN } from "../liveReducer";
import { Field, Select, Seg, Switch } from "./formkit";
import { buildModelMenu } from "./modelMenu";
import { MemoryPage } from "./MemoryPage";
import { ConnectionsPage } from "./ConnectionsPage";
import { CustomizationPage } from "./CustomizationPage";
import { FactorySettings } from "./FactorySettings";
import { StepLimitSetting } from "./StepLimitSetting";
import { ReconnectSettings } from "./ReconnectSettings";
import { SoundSettings } from "./SoundSettings";
import { ProviderEditor } from "./ProviderEditor";
import { ProvidersPage } from "./ProvidersPage";
import { FusionSettings } from "./FusionSettings";
import { StorageSettings } from "./StorageSettings";
import type { MenuItem } from "./ui";
import "../styles/settings.css";

const PAGES = [
  { id: "appearance", label: "外观", icon: <Brush size={15} /> },
  { id: "providers", label: "账号与模型", icon: <KeyRound size={15} /> },
  { id: "sessions", label: "会话", icon: <Layers size={15} /> },
  { id: "fusion", label: "Fusion", icon: <Sparkles size={15} /> },
  { id: "customization", label: "自定义", icon: <SlidersHorizontal size={15} /> },
  { id: "memory", label: "共享记忆", icon: <Brain size={15} /> },
  { id: "programs", label: "外部程序", icon: <Cpu size={15} /> },
  { id: "projects", label: "项目", icon: <FolderOpen size={15} /> },
  { id: "connections", label: "插件", icon: <Layers size={15} /> },
  { id: "storage", label: "存储", icon: <HardDrive size={15} /> },
  { id: "about", label: "关于", icon: <Info size={15} /> },
];

function AppearancePage() {
  const s = useStore();
  return (
    <>
      <Field label="主题" hint="浅色是 Logo 的反色。跟随系统时，随 Windows 的深浅色切换。">
        <Seg<ThemeChoice>
          value={s.themeChoice}
          onChange={s.setTheme}
          options={[
            { id: "system", label: "跟随系统" },
            { id: "light", label: "浅色" },
            { id: "dark", label: "深色" },
          ]}
        />
      </Field>
      <Field label="文字大小" hint="对话正文和界面文字一起缩放。">
        <Seg<FontScale>
          value={s.fontScale}
          onChange={s.setFontScale}
          options={[
            { id: "sm", label: "小" },
            { id: "md", label: "默认" },
            { id: "lg", label: "大" },
          ]}
        />
      </Field>
    </>
  );
}

function DefaultsPage() {
  const s = useStore();
  const model = findModel(s.models, s.defaults.model);
  const engine = engineOfSource(model?.source ?? "chatgpt");
  const levels = permissionsFor(engine);
  const pick = (field: "reasoning" | "autonomy" | "mode", list: { value: string; label: string; hint?: string }[]): MenuItem[] =>
    list.map((o) => ({ id: o.value, label: o.label, description: o.hint, checked: s.defaults[field] === o.value, onSelect: () => s.setDefault(field, o.value) }));
  const none = !!model && model.efforts.length === 0;
  return (
    <>
      <h3 className="settings-section-title">新会话默认值</h3>
      <p className="page-note">保存后供新会话使用，不随单个会话的选择改变。</p>
      <Field label="模型">
        <Select
          value={model?.label ?? s.defaults.model}
          minWidth={280}
          items={buildModelMenu({
            models: s.models,
            providers: s.providers,
            devin: s.devin,
            chatgpt: s.chatgpt,
            current: s.defaults.model,
            hidden: s.hiddenModels,
            onPick: (id) => s.setDefault("model", id),
            onManage: (edit) => s.openSettings("providers", edit),
            onChatGPTLogin: (forceConsent) => void s.chatgptLogin({ forceConsent }),
          })}
        />
      </Field>
      <Field label="推理" hint={none ? "此模型不提供推理档位。" : undefined}>
        <Select value={none ? "—" : effortLabel(s.defaults.reasoning || model?.defaultEffort || "") || "—"} disabled={none} items={pick("reasoning", reasoningChoices(model))} />
      </Field>
      <Field
        label="权限"
        hint={
          engine === "devin" ? "使用 Devin 的权限策略。" : "完全访问不提供操作系统沙箱。"
        }
      >
        <Select value={levels.find((r) => r.value === s.defaults.autonomy)?.label ?? s.defaults.autonomy} items={pick("autonomy", levels)} />
      </Field>
      <Field label="模式">
        <Select value={MODES.find((r) => r.value === s.defaults.mode)?.label ?? s.defaults.mode} items={pick("mode", MODES)} />
      </Field>
    </>
  );
}

function SessionsPage() {
  const s = useStore();
  return (
    <>
      <div className="sessions-columns">
      <section><DefaultsPage /></section>
      <section>
      <h3 className="settings-section-title">会话行为</h3>
      <Field
        label="运行中按 Enter"
        hint="Ctrl+Enter 使用另一种方式。"
      >
        <Seg<FollowUp>
          value={s.followUp}
          onChange={s.setFollowUp}
          options={[
            { id: "queue", label: "排队" },
            { id: "steer", label: "立即引导" },
          ]}
        />
      </Field>
      <Field
        label="自动压缩上下文"
        hint="WittyHarness 在接近上下文上限时生成摘要，完整对话记录保留。"
      >
        <Switch on={s.compactAuto} onChange={s.setCompactAuto} label="自动压缩上下文" />
      </Field>
      {s.compactAuto && (
        <Field
          label="压缩时机"
          hint="上下文窗口的 50–95%，默认 85%。"
        >
          <div className="stepper">
            <button type="button" aria-label="提早压缩" onClick={() => s.setCompactAt(s.compactAt - 5)} disabled={s.compactAt <= COMPACT_AT_MIN}>
              <Minus size={14} />
            </button>
            <span className="num">{s.compactAt}%</span>
            <button type="button" aria-label="推迟压缩" onClick={() => s.setCompactAt(s.compactAt + 5)} disabled={s.compactAt >= COMPACT_AT_MAX}>
              <Plus size={14} />
            </button>
          </div>
        </Field>
      )}
      <StepLimitSetting />
      <Field label="同时打开的会话上限" hint="超出时自动关闭最久未用的空闲会话，内容仍保留。">
        <div className="stepper">
          <button type="button" aria-label="减少" onClick={() => s.setMaxOpen(s.maxOpen - 1)} disabled={s.maxOpen <= 1}>
            <Minus size={14} />
          </button>
          <span className="num">{s.maxOpen}</span>
          <button type="button" aria-label="增加" onClick={() => s.setMaxOpen(s.maxOpen + 1)} disabled={s.maxOpen >= 16}>
            <Plus size={14} />
          </button>
        </div>
      </Field>
      </section>
      </div>
      <ReconnectSettings />
      <SoundSettings />
    </>
  );
}

/** A program WittyCode starts: found automatically, or a path the user gives. The typed path is saved when the field
 *  is left (or on Enter), not on every key: changing it makes Python look for the program again. */
function ProgramPath({ label, autoHint, found, path, onPath, browse }: { label: string; autoHint: string; found: string; path: string; onPath: (p: string) => void; browse?: () => void }) {
  const [custom, setCustom] = useState(path !== "");
  const [draft, setDraft] = useState(path);
  useEffect(() => setDraft(path), [path]);
  const auto = path === "" && !custom;
  const commit = (value: string) => value.trim() !== path && onPath(value.trim());
  return (
    <>
      <Field label={label} hint={auto ? autoHint : "使用你指定的文件。"}>
        <Seg
          value={auto ? "auto" : "custom"}
          onChange={(v) => {
            setCustom(v === "custom");
            onPath(v === "auto" ? "" : found);
          }}
          options={[
            { id: "auto", label: "自动检测" },
            { id: "custom", label: "指定路径" },
          ]}
        />
      </Field>
      <div className="pathbox selectable">{(auto ? found : path) || "（没有找到）"}</div>
      {!auto && (
        <div className="row-btns">
          <input
            className="textin selectable"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.currentTarget.value)}
            onKeyDown={(e) => e.key === "Enter" && commit(e.currentTarget.value)}
            aria-label={`${label} 路径`}
          />
          {browse && (
            <button type="button" className="btn" onClick={browse}>
              浏览…
            </button>
          )}
        </div>
      )}
    </>
  );
}

function ProgramsPage() {
  const s = useStore();
  return (
    <div className="programs">
      <p className="page-note">WittyCode 自己的代理循环（ChatGPT Plan、API Provider）不需要任何别的程序。Devin 和 Factory CLI 都是可选连接；在这里指定程序，再按需启用。</p>
      <FactorySettings program />
      <section className="pcard program-path">
        <ProgramPath label="Devin 命令行" autoHint="自动检测：在 PATH 和 Devin 桌面版的安装目录里找 devin。" found={s.devin.cliPath ?? ""} path={s.devinPath} onPath={s.setDevinPath} />
      </section>
      <p className="page-note">改了路径后，已经打开的会话继续用旧进程；新开的会话才用新路径。</p>
    </div>
  );
}

function ProjectsPage() {
  const s = useStore();
  return (
    <>
      <p className="page-note">侧边栏里的项目文件夹。从列表中移除不会删除磁盘上的文件夹，也不会删除已保存的会话。</p>
      <div className="plist">
        {s.projects.map((p) => (
          <div key={p.id} className="prow">
            <div className="prow-text">
              <div className="pname">{p.name}</div>
              <div className="ppath selectable">{p.path}</div>
            </div>
            <button type="button" className="icon-btn" aria-label={`移除 ${p.name}`} title="从列表中移除" onClick={() => s.removeProject(p.id)}>
              <Trash2 size={15} />
            </button>
          </div>
        ))}
        {s.projects.length === 0 && <div className="snote">还没有项目。</div>}
      </div>
      <div className="row-btns">
        <button type="button" className="btn" onClick={s.addProject}>
          添加项目文件夹…
        </button>
      </div>
    </>
  );
}

const SHORTCUTS: [string, string][] = [
  ["Enter", "发送；运行中默认排队"],
  ["Ctrl+Enter", "运行中默认立即引导"],
  ["Shift+Enter", "换行"],
  ["Esc", "停止当前任务"],
  ["Ctrl+N", "新会话"],
  ["Ctrl+O", "添加项目文件夹"],
  ["Ctrl+W", "关闭当前会话"],
  ["Ctrl+L", "聚焦输入框"],
  ["Ctrl+B", "收起 / 展开侧栏"],
  ["Ctrl+End", "跳到最新消息"],
  ["Ctrl+,", "设置"],
  ["F5", "刷新会话列表"],
];

function ShortcutsPage() {
  return (
    <div className="shortcuts">
      {SHORTCUTS.map(([k, v]) => (
        <div key={k} className="sc">
          <kbd>{k}</kbd>
          <span>{v}</span>
        </div>
      ))}
    </div>
  );
}

function AboutLink({ text, href, action }: { text: string; href: string; action: string }) {
  return (
    <b className="about-link">
      {/* Keep the value outside the anchor so mouse selection never opens the link. */}
      <span className="about-link-text selectable">{text}</span>
      <a className="icon-btn" href={href} aria-label={action} title={action}>
        <ExternalLink size={14} aria-hidden />
      </a>
    </b>
  );
}

function AboutPage() {
  const say = useStore((s) => s.say);
  const contact = aboutContact(import.meta.env.VITE_WITTYCODE_CONTACT_EMAIL);
  return (
    <>
    <div className="about">
      <LogoVertical width={132} />
      <div className="about-text">
        <h3>WittyCode</h3>
        <p className="muted-text">Windows 编程代理桌面应用。</p>
        <div className="kvs">
          <div>
            <span>版本</span>
            <b>{version}</b>
          </div>
          <div>
            <span>联系方式</span>
            <AboutLink {...contact} action="打开联系方式" />
          </div>
          <div>
            <span>项目 GitHub</span>
            <AboutLink text={PROJECT_GITHUB_URL} href={PROJECT_GITHUB_URL} action="打开项目 GitHub" />
          </div>
          <div>
            <span>日志</span>
            <b className="selectable">%APPDATA%\WittyCode\WittyCode.log</b>
          </div>
        </div>
        {!LIVE && (
          <div className="row-btns">
            <button type="button" className="btn" onClick={() => say("（演示）这里会打开日志文件夹。")}>
              打开日志文件夹
            </button>
          </div>
        )}
      </div>
    </div>
    <h3 className="settings-section-title">快捷键</h3>
    <ShortcutsPage />
    </>
  );
}

export function SettingsHost() {
  const page = useStore((s) => s.settingsPage);
  const editing = useStore((s) => s.providerEdit);
  const saving = useStore((s) => s.settingsSaving);
  const open = useStore((s) => s.openSettings);
  // (what agents wrote that the user has not looked at, and the suggestions that wait for the user's word)
  const unseen = useStore((s) => s.memory.unseen + s.memory.pending);
  useEffect(() => {
    if (!page) return;
    // Esc first leaves the provider editor, then the settings.
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || useStore.getState().dialog) return;
      if (useStore.getState().providerEdit) useStore.getState().setProviderEdit(null);
      else open(null);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [page, open]);
  if (!page) return null;
  const target = page === "defaults" ? "sessions" : page === "shortcuts" ? "about" : page;
  const current = PAGES.find((p) => p.id === target) ?? PAGES[0]!;
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && open(null)}>
      <div className="settings" role="dialog" aria-modal="true" aria-label="设置">
        <nav className="settings-nav">
          <div className="settings-title">设置</div>
          {PAGES.map((p) => (
            <button key={p.id} type="button" className={`snav${p.id === current.id ? " on" : ""}`} aria-current={p.id === current.id} disabled={saving} onClick={() => open(p.id)}>
              {p.icon}
              {p.label}
              {p.id === "memory" && unseen > 0 && (
                <span className="snav-badge" aria-label={`${unseen} 条新的`}>
                  {unseen}
                </span>
              )}
            </button>
          ))}
          <div className="settings-foot">{saving ? "正在保存规则…" : <>设置自动保存<br />规则和配置需确认保存</>}</div>
        </nav>
        <section className="settings-body">
          <header>
            <h2>{current.label}</h2>
            <button type="button" className="icon-btn" aria-label="关闭" disabled={saving} onClick={() => open(null)}>
              <X size={16} />
            </button>
          </header>
          <div className="settings-scroll">
            {current.id === "appearance" && <AppearancePage />}
            {current.id === "providers" && (editing ? <ProviderEditor key={editing} editId={editing} /> : <ProvidersPage />)}
            {current.id === "sessions" && <SessionsPage />}
            {current.id === "fusion" && <FusionSettings />}
            {current.id === "customization" && <CustomizationPage />}
            {current.id === "memory" && <MemoryPage />}
            {current.id === "programs" && <ProgramsPage />}
            {current.id === "projects" && <ProjectsPage />}
            {current.id === "about" && <AboutPage />}
            {current.id === "connections" && <ConnectionsPage />}
            {current.id === "storage" && <StorageSettings />}
          </div>
        </section>
      </div>
    </div>
  );
}
