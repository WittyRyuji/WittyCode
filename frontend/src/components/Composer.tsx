import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, ChevronDown, Paperclip, Square } from "lucide-react";
import type { FollowUp } from "../protocol";
import { currentSession, landingProjectId, useStore } from "../store";
import { MODES, effortLabel, permissionsFor, reasoningChoices } from "../lib/vocab";
import { engineOfSource, sourceLabel, sourceTag } from "../lib/models";
import { CHATGPT_USAGE_URL, chatgptReady } from "../lib/chatgpt";
import { LIVE } from "../mode";
import { commandNamed, fromSkills, parseSlash, suggestions, unavailable, type Scene, type SlashCommand } from "../lib/commands";
import type { Engine, SessionSettings } from "../types";
import { AttachmentCard } from "./Attachments";
import { buildModelMenu } from "./modelMenu";
import { QueueStrip } from "./QueueStrip";
import { MenuButton, Tip, type MenuItem } from "./ui";

/** What a message typed while a turn runs can become: held until the turn is over, or given to the turn now. */
const HOW: Record<FollowUp, { label: string; what: string }> = {
  queue: { label: "排队", what: "排队：本轮结束后自动发出，发出前可以撤回" },
  steer: { label: "引导", what: "立即引导：现在交给正在运行的这一轮，模型在下一步就读到" },
};
const otherHow = (how: FollowUp): FollowUp => (how === "queue" ? "steer" : "queue");
/** What the box says while it is empty: what to do and which keys do what (it used to be a line of its own under the box). */
const READY_PLACEHOLDER = "输入消息…  Enter 发送 · Shift+Enter 换行 · 可粘贴图片、拖入文件";
const runningPlaceholder = (how: FollowUp) => `本轮运行中…  Enter ${HOW[how].label} · Ctrl+Enter ${HOW[otherHow(how)].label} · Esc 停止`;

/** `bare`: the label is not shown (the value says it: a model, a level, a permission); it stays as the name of the button and its tooltip. */
function Chip({ label, value, items, disabled, tag, title, extra, bare }: { label: string; value: string; items: MenuItem[]; disabled?: boolean; tag?: string; title?: string; extra?: string; bare?: boolean }) {
  return (
    <MenuButton className={`chip${extra ? ` ${extra}` : ""}`} items={items} up disabled={disabled} minWidth={250} title={title ?? (bare ? label : undefined)} ariaLabel={`${label}：${value}`}>
      {!bare && <span className="chip-k">{label}</span>}
      <span className="chip-v">{value}</span>
      {tag && <span className="src-tag">{tag}</span>}
      <ChevronDown size={13} className="chip-c" />
    </MenuButton>
  );
}

/** `live`: the open session's state, or null when the controls edit the defaults for new sessions. */
function Controls({ values, disabled, live, onChoose }: { values: SessionSettings; disabled: boolean; live: { running: boolean; fresh: boolean; engine?: Engine } | null; onChoose: (f: keyof SessionSettings, v: string) => void }) {
  const { models, providers, devin, chatgpt, hiddenModels } = useStore();
  const openSettings = useStore((s) => s.openSettings);
  const chatgptLogin = useStore((s) => s.chatgptLogin);
  const model = models.find((m) => m.id === values.model);
  // An open session knows its engine even before the model list has arrived.
  const engine = live?.engine ?? engineOfSource(model?.source ?? "chatgpt");
  const levels = permissionsFor(engine);
  // No level chosen: the model's own default is what runs.
  const reasoning = values.reasoning || model?.defaultEffort || "";
  const pick = <T extends { value: string; label: string; hint?: string }>(field: keyof SessionSettings, list: T[], current: string = values[field]): MenuItem[] =>
    list.map((o) => ({ id: o.value, label: o.label, description: o.hint, checked: current === o.value, onSelect: () => onChoose(field, o.value) }));
  const noReasoning = !!model && model.efforts.length === 0;
  const shown = (text: string) => text || "—";
  return (
    <div className="controls" role="group" aria-label="会话设置">
      <Chip
        label="模型"
        bare
        extra="chip-model"
        value={model?.label ?? shown(values.model)}
        tag={model ? sourceTag(model.source) : undefined}
        title={model ? `${model.label}${sourceLabel(model.source, providers) ? ` · ${sourceLabel(model.source, providers)}` : ""}` : undefined}
        disabled={disabled}
        items={buildModelMenu({
          models,
          providers,
          devin,
          chatgpt,
          current: values.model,
          hidden: hiddenModels,
          engine: live ? engine : undefined,
          running: live?.running,
          fresh: live?.fresh,
          onPick: (id) => onChoose("model", id),
          onManage: (edit) => openSettings("providers", edit),
          onChatGPTLogin: (forceConsent) => void chatgptLogin({ forceConsent }),
        })}
      />
      <Chip
        label="推理"
        bare
        extra="chip-reasoning"
        value={noReasoning ? "—" : shown(effortLabel(reasoning))}
        disabled={disabled || noReasoning}
        title={noReasoning ? "这个模型没有推理档位" : "推理强度"}
        items={pick("reasoning", reasoningChoices(model), reasoning)}
      />
      <Chip label="权限" bare extra="chip-permission" title="权限：不经询问能做多少事" value={shown(levels.find((r) => r.value === values.autonomy)?.label ?? values.autonomy)} disabled={disabled} items={pick("autonomy", levels)} />
      <Chip label="模式" extra="chip-mode" value={shown(MODES.find((r) => r.value === values.mode)?.label ?? values.mode)} disabled={disabled} items={pick("mode", MODES)} />
    </div>
  );
}

/** OpenAI's guideline: say that the plan is in use, near the composer, with the way to manage it. It sits at the right end of
 *  the line under the box, not among the controls: there it widened the row until the send button fell onto a line of its own. */
function PlanBadge() {
  const chatgpt = useStore((s) => s.chatgpt);
  const ready = chatgptReady(chatgpt);
  return (
    <span className={`plan-badge${ready ? "" : " off"}`}>
      <span className="pdot" />
      {ready ? "Using ChatGPT Plan" : "ChatGPT 未登录"}
      <a className="inline-link" href={CHATGPT_USAGE_URL}>
        Manage usage
      </a>
    </span>
  );
}

/** The commands for what is typed after a slash. A command that cannot be run now is still listed, with the reason. */
function SlashMenu({ items, active, scene, onPick, onHover }: { items: SlashCommand[]; active: number; scene: Scene; onPick: (c: SlashCommand) => void; onHover: (i: number) => void }) {
  const lit = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    lit.current?.scrollIntoView({ block: "nearest" });
  }, [active, items.length]);
  return (
    <div className="slash-menu" role="listbox" aria-label="命令">
      {items.map((c, i) => {
        // (a skill or a command of the user's can always be named; the page's own may not be runnable now)
        const why = c.kind ? null : unavailable(c.name, scene, c.args ? "x" : "");
        // (the page's own commands come first: a line sets the user's skills and commands apart from them)
        const apart = !!c.kind && i > 0 && !items[i - 1]!.kind;
        return (
          <Fragment key={c.name}>
            {apart && <div className="slash-sep" role="separator" />}
            <button
              ref={i === active ? lit : undefined}
              type="button"
              role="option"
              aria-selected={i === active}
              className={`slash-item${i === active ? " on" : ""}${why ? " off" : ""}${c.kind ? ` ${c.kind}` : ""}`}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => onHover(i)}
              onClick={() => onPick(c)}
            >
              <span className="sl-name">/{c.name}</span>
              {c.args && <span className="sl-args">{c.args}</span>}
              {c.kind && <span className="sl-kind">{c.kind === "skill" ? "技能" : "命令"}</span>}
              <span className="sl-hint">{why ?? c.hint}</span>
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}

/** Where the first message of a new conversation will go: the project, with the others (and adding one) in the menu. */
function ProjectChip({ disabled }: { disabled: boolean }) {
  const projects = useStore((s) => s.projects);
  const chosen = useStore(landingProjectId);
  const setProject = useStore((s) => s.setLandingProject);
  const addProject = useStore((s) => s.addProject);
  const items: MenuItem[] = [
    ...projects.map((p) => ({ id: p.id, label: p.name, description: p.path, checked: p.id === chosen, onSelect: () => setProject(p.id) })),
    ...(projects.length ? [{ id: "sep", separator: true } as MenuItem] : []),
    { id: "add", label: "添加项目文件夹…", onSelect: addProject },
  ];
  const project = projects.find((p) => p.id === chosen);
  return <Chip label="项目" extra="chip-project" value={project?.name ?? "—"} title={project?.path} disabled={disabled} items={items} />;
}

export function Composer() {
  const session = useStore(currentSession);
  const defaults = useStore((s) => s.defaults);
  const landing = useStore((s) => s.landing);
  const { setDraft, setLandingDraft, startSession, adoptLanding, send, stop, choose, attachFiles, pickAttachments, removeAttachment, say, reconnect, compact, startGoal } = useStore.getState();
  const area = useRef<HTMLTextAreaElement>(null);
  // The command menu: which entry is lit, and the draft for which the user closed it with Esc.
  const [pick, setPick] = useState(0);
  const [closed, setClosed] = useState("");

  const mark = session?.mark;
  const running = mark === "running" || mark === "attention";
  const dead = mark === "dead";
  // A session that is still connecting is typed into like any other: it connects while the user types, and what is
  // sent before it is ready is kept and goes out by itself (Python's queue).
  const connecting = mark === "opening";
  // With no conversation open this is the first message box: nothing exists in Python until a message is sent from it.
  const editable = session ? mark === "ready" || running || connecting : !landing.starting;
  const draft = session ? session.draft : landing.draft;
  const attachments = session?.attachments ?? [];
  const hasContent = draft.trim().length > 0 || attachments.length > 0;
  const canSend = session ? (mark === "ready" || connecting) && hasContent : hasContent && !landing.starting;
  const followUp = useStore((s) => s.followUp);

  const placeholder = !session
    ? landing.starting
      ? "正在连接…"
      : READY_PLACEHOLDER
    : dead
      ? "连接已断开，重新连接后才能继续。"
      : running
        ? runningPlaceholder(followUp)
        : READY_PLACEHOLDER;

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [draft, session?.id]);

  useEffect(() => {
    const focus = () => area.current?.focus();
    window.addEventListener("wc:focus-composer", focus);
    return () => window.removeEventListener("wc:focus-composer", focus);
  }, []);

  useEffect(() => {
    if (editable) area.current?.focus();
  }, [session?.id, editable]);

  const add = (files: File[]) => {
    if (!editable) return say("现在不能添加附件。");
    if (session) return void attachFiles(session.id, files);
    // (a file needs a conversation to be put in: it is opened now, with what is typed so far)
    void adoptLanding().then((id) => void (id && attachFiles(id, files)));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (menu.length && !e.nativeEvent.isComposing) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setPick((active + (e.key === "ArrowDown" ? 1 : menu.length - 1)) % menu.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pickCommand(menu[active]!);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setClosed(draft);
        return;
      }
    }
    // A command typed out with its text (`/goal fix it`) is run, not sent; a slash line that names none is a message.
    const typed = parseSlash(draft);
    const command = typed && !typed.typing ? commandNamed(typed.name) : undefined;
    if (command && typed && e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      runCommand(command, typed.args);
      return;
    }
    if (!session) {
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        void startSession();
      }
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      // Ctrl+Enter does the other one of the two things a message typed during a turn can do.
      send(session.id, e.ctrlKey || e.metaKey ? otherHow(followUp) : followUp);
    } else if (e.key === "Escape" && running) {
      e.preventDefault();
      stop(session.id);
    }
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const files = [...e.clipboardData.files];
    if (files.length) {
      e.preventDefault();
      add(files);
    }
  };

  const values = session?.settings ?? defaults;
  const chipsDisabled = !!session && dead;
  // An open session knows its engine even before the model list has arrived (the demo's sessions do not say: their model does).
  const modelSource = useStore((s) => s.models.find((m) => m.id === values.model)?.source);
  // (the own loop also runs the API providers' models: the plan's badge and usage are for the plan's models only)
  const onPlan = !!session && (session.engine ?? engineOfSource(modelSource ?? "chatgpt")) === "chatgpt" && !modelSource?.startsWith("api:");

  const scene: Scene = { session: !!session, engine: session?.engine ?? engineOfSource(modelSource ?? "chatgpt"), running, plan: values.mode === "spec" };
  // The skills and commands of the user's come from the files, read again whenever a slash is started (one made a moment ago is there).
  const userSkills = useStore((s) => s.skills);
  const extra = useMemo(() => fromSkills(userSkills), [userSkills]);
  const slashOpen = editable && !!parseSlash(draft)?.typing;
  useEffect(() => {
    if (slashOpen) useStore.getState().refreshSkills(session?.id);
  }, [slashOpen, session?.id]);
  const menu = editable && closed !== draft ? suggestions(draft, extra) : [];
  const active = Math.min(pick, Math.max(menu.length - 1, 0));
  const setText = (text: string) => (session ? setDraft(session.id, text) : setLandingDraft(text));
  const runCommand = (command: SlashCommand, args: string) => {
    const why = unavailable(command.name, scene, args);
    if (why) return say(why); // (what was typed stays: it may be fixed)
    setText("");
    if (command.name === "plan") {
      const on = values.mode !== "spec";
      choose("mode", on ? "spec" : "auto");
      const own = scene.engine === "chatgpt";
      return say(on ? (own ? "已切到 Plan：模型只读不改，先出方案；满意了再发 /plan 回到 Auto，让它动手。" : "已切到 Plan：模型先出方案，批准后再动手。") : "已回到 Auto：模型直接动手。");
    }
    if (session && command.name === "compact") return void compact(session.id);
    if (session && command.name === "goal") return void startGoal(session.id, args);
  };
  /** The send button with a command in the box runs it (as Enter does); `false`: the box holds a message. */
  const runTyped = () => {
    const typed = parseSlash(draft);
    const command = typed ? commandNamed(typed.name) : undefined;
    if (!typed || !command) return false;
    runCommand(command, typed.args);
    return true;
  };
  /** Picked from the menu: a skill or a command of the user's only writes its name (the message that names it goes out as any
   *  message); one of the page's own that takes text waits for it, the others run. */
  const pickCommand = (command: SlashCommand) => {
    if (command.kind || command.args) {
      setText(`/${command.name} `);
      area.current?.focus();
    } else runCommand(command, "");
  };

  return (
    <div className="composer-wrap">
      {session && <QueueStrip session={session} />}
      <div className={`composer${!editable ? " off" : ""}`}>
        {menu.length > 0 && <SlashMenu items={menu} active={active} scene={scene} onPick={pickCommand} onHover={setPick} />}
        {attachments.length > 0 && (
          <div className="att-row">
            {attachments.map((a) => (
              <AttachmentCard key={a.id} a={a} onRemove={() => session && removeAttachment(session.id, a.id)} />
            ))}
          </div>
        )}
        <textarea
          ref={area}
          className="input selectable"
          rows={1}
          value={draft}
          placeholder={placeholder}
          disabled={!editable}
          onChange={(e) => {
            setPick(0);
            setClosed("");
            setText(e.target.value);
          }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          aria-label="消息"
        />
        <div className="composer-bar">
          <Tip text="添加图片或文件（也可以粘贴、拖入）" side="top">
            {LIVE ? (
              // Python's own file dialog: it reads the files itself.
              <button
                type="button"
                className={`icon-btn attach${!editable ? " disabled" : ""}`}
                aria-label="添加图片或文件"
                disabled={!editable}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => (session ? void pickAttachments(session.id) : void adoptLanding().then((id) => void (id && pickAttachments(id))))}
              >
                <Paperclip size={16} />
              </button>
            ) : (
              <label className={`icon-btn attach${!editable ? " disabled" : ""}`} onMouseDown={(e) => e.preventDefault()}>
                <Paperclip size={16} />
                <input
                  type="file"
                  multiple
                  hidden
                  disabled={!editable}
                  onChange={(e) => {
                    add([...(e.target.files ?? [])]);
                    e.target.value = "";
                  }}
                />
              </label>
            )}
          </Tip>
          <Controls
            values={values}
            disabled={chipsDisabled || (!session && landing.starting)}
            live={session ? { running, fresh: session.turns.length === 0, engine: session.engine } : null}
            onChoose={choose}
          />
          <span className="spacer" />
          <div className="bar-actions">
          {running ? (
            <>
              {hasContent &&
                [otherHow(followUp), followUp].map((how) => (
                  <Tip key={how} text={`${HOW[how].what}${how === followUp ? "（Enter）" : "（Ctrl+Enter）"}`} side="top">
                    <button type="button" className={`send how${how === followUp ? "" : " alt"}`} onClick={() => session && send(session.id, how)} aria-label={HOW[how].label}>
                      {HOW[how].label}
                    </button>
                  </Tip>
                ))}
              <button type="button" className="send stop" onClick={() => session && stop(session.id)}>
                <Square size={11} fill="currentColor" /> 停止
              </button>
            </>
          ) : (
            <button type="button" className="send" disabled={!canSend} onClick={() => (runTyped() ? undefined : session ? send(session.id) : void startSession())} aria-label="发送">
              发送 <ArrowUp size={15} />
            </button>
          )}
          </div>
        </div>
      </div>
      {/* The line under the box exists only when it has something to hold: the project of a new conversation, a note about
          the next message, the way to reconnect, the plan in use. The key hints are in the box. */}
      {(!session || (dead && session) || (session?.handoff && !running) || onPlan) && (
        <div className={`hint${session?.handoff && !running ? " handoff" : ""}`}>
          {!session && <ProjectChip disabled={landing.starting} />}
          {session && dead ? (
            <span className="hint-text">
              连接已断开 ·{" "}
              <button type="button" className="linkbtn" onClick={() => reconnect(session.id)}>
                重新连接
              </button>
            </span>
          ) : session?.handoff && !running ? (
            <span className="hint-text">
              下一条消息会带上之前 {session.handoff.carried} 条对话的文字{session.handoff.truncated ? "（节选）" : ""}
              {session.handoff.attachments ? `和 ${session.handoff.attachments} 个附件` : ""}，交给 {session.handoff.to}
            </span>
          ) : (
            <span className="hint-text" />
          )}
          {onPlan && <PlanBadge />}
        </div>
      )}
    </div>
  );
}
