import { useId } from "react";
import { currentSession, useStore } from "../store";
import type { ContextState, Speed, Usage } from "../types";
import { cacheHitRate, formatCount, formatSpeed, formatTokens, usageTotal } from "../lib/format";
import { StateDot, Tip } from "./ui";
import { usageGradient, usagePercent, usageSlices } from "../lib/usage";
import type { ModelChoice } from "../types";

const UNKNOWN = "—";

function ContextTip({ c }: { c: ContextState }) {
  return (
    <div className="tip-lines">
      <b>Context 上下文</b>
      <div>本会话发给模型的内容（系统提示、工具说明和全部对话）占用的 token。</div>
      <div>
        已用：{c.used === null ? (c.compacted ? "—（刚压缩过对话，下一次模型调用后更新）" : "—（发送一条消息后显示）") : c.used.toLocaleString()}
      </div>
      <div>
        窗口：{c.window > 0 ? `${c.window.toLocaleString()}（${c.model ? `${c.model} 的` : ""}上下文窗口）` : "—（还不知道这个模型的上下文窗口）"}
      </div>
    </div>
  );
}

/** `Context 10K/999K ▮▮▮▯▯▯`: how much of the model's window the conversation fills. Nothing else is drawn on the bar.
 *  A window that is not known yet (0) reads `Context 12K/—` over an empty bar. */
function ContextMeter({ c }: { c: ContextState }) {
  const known = c.window > 0;
  const fill = c.used === null || !known ? 0 : Math.min(1, c.used / c.window);
  return (
    <Tip text={<ContextTip c={c} />} className="ctxmeter">
      <span className="k">Context</span>
      <span className="v">
        {c.used === null ? UNKNOWN : formatTokens(c.used)}/{known ? formatTokens(c.window) : UNKNOWN}
      </span>
      <span className="meter" aria-hidden>
        {known && <span className="meter-fill" style={{ width: `${fill * 100}%` }} />}
      </span>
    </Tip>
  );
}

function ModelUsageChart({ total, byModel, models }: { total: number; byModel?: Record<string, number>; models: ModelChoice[] }) {
  const prefix = useId();
  const slices = usageSlices(total, byModel, models);
  if (!slices.length) return null;
  let angle = -Math.PI / 2;
  const wedges = slices.map((slice) => {
    const start = angle;
    angle += slice.share * Math.PI * 2;
    return `M60,60 L${60 + 60 * Math.cos(start)},${60 + 60 * Math.sin(start)} A60,60 0 ${slice.share > 0.5 ? 1 : 0},1 ${60 + 60 * Math.cos(angle)},${60 + 60 * Math.sin(angle)} Z`;
  });
  return (
    <div className="usage-models">
      <svg className="usage-pie" viewBox="0 0 120 120" role="img" aria-label={`模型 Token 占比：${slices.map((s) => `${s.label} ${usagePercent(s.share)}`).join("，")}`}>
        <defs>{slices.map((s, i) => {
          const [start, end] = usageGradient(s.color);
          return <linearGradient key={s.id} id={`${prefix}-${i}`} x1="0" y1="0" x2="1" y2="1"><stop stopColor={start} /><stop offset="1" stopColor={end} /></linearGradient>;
        })}</defs>
        {slices.map((s, i) => slices.length === 1
          ? <circle key={s.id} cx="60" cy="60" r="60" fill={`url(#${prefix}-${i})`} />
          : <path key={s.id} d={wedges[i]} fill={`url(#${prefix}-${i})`} />)}
      </svg>
      <ul className="usage-legend">
        {slices.map((s) => (
          <li key={s.id} data-model={s.id} title={`${s.label} · ${s.tokens.toLocaleString()} token`}>
            <span className="usage-swatch" style={{ backgroundColor: s.color, backgroundImage: `linear-gradient(135deg, ${usageGradient(s.color).join(",")})` }} aria-hidden />
            <span className="usage-model-name">{s.label}</span>
            <span className="usage-percent">{usagePercent(s.share)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function UsageTip({ u, byModel, models }: { u: Usage; byModel?: Record<string, number>; models: ModelChoice[] }) {
  const rate = cacheHitRate(u);
  // A service that does not report cache writes (OpenAI's, Devin's) has no such row, rather than a row of dashes.
  const rows: [string, string][] = [
    ["合计", usageTotal(u).toLocaleString()],
    ["In", u.input.toLocaleString()],
    ["Out", u.output.toLocaleString()],
    ...(u.cacheWrite === null ? [] : [["Cache Write", u.cacheWrite.toLocaleString()] as [string, string]]),
    ["Cache Read", u.cacheRead.toLocaleString()],
    ["Cache 命中率", rate === null ? UNKNOWN : `${(rate * 100).toFixed(1)}%`],
  ];
  return (
    <div className="tip-lines usage-tip">
      <ModelUsageChart total={usageTotal(u)} byModel={byModel} models={models} />
      {Object.values(byModel ?? {}).reduce((sum, n) => sum + n, 0) < usageTotal(u) && <div className="tip-note">旧记录未保存模型明细的部分显示为「未归属」，不会猜填。已知明细保存在本地，重启后保留。</div>}
      <b>Token 用量（整个对话累计）</b>
      <dl className="tip-table">
        {rows.map(([k, v]) => (
          <div key={k} className="tip-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function SpeedTip({ s }: { s: Speed }) {
  const rows: [string, string][] = [
    ["TPS", `${formatSpeed(s.tps)} token/秒`],
    ["输出", `${s.tokens.toLocaleString()} token`],
    ["模型用时（不含工具、确认）", `${s.seconds.toFixed(1)} 秒`],
    ...(s.first === null ? [] : [["首字等待", `${s.first.toFixed(1)} 秒`] as [string, string]]),
    ...(s.genTps === null ? [] : [["不含首字等待", `${formatSpeed(s.genTps)} token/秒`] as [string, string]]),
  ];
  return (
    <div className="tip-lines">
      <b>估算值：输出速度（最近一次能测的一轮）</b>
      <dl className="tip-table">
        {rows.map(([k, v]) => (
          <div key={k} className="tip-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** `TPS ≈42.3`: about how many tokens a second the model wrote in the latest turn that can tell (an estimate: the tokens the
 *  engine counted over the time the model had the turn, not a measurement of the stream). Between Context and Token. */
function SpeedText({ s }: { s: Speed | null | undefined }) {
  if (!s) {
    return (
      <Tip text={<div className="tip-lines"><b>输出速度</b><div>还没有测到：需要一轮输出不少于 10 个 token、用时不少于半秒。</div></div>} className="speed">
        <span className="k">TPS</span>
        <span className="v">{UNKNOWN}</span>
      </Tip>
    );
  }
  return (
    <Tip text={<SpeedTip s={s} />} className="speed">
      <span className="k">TPS</span>
      <span className="v">≈{formatSpeed(s.tps)}</span>
    </Tip>
  );
}

/** `Token 合计 69K In 0.4K Out 4K`: the whole conversation's cumulative use; hover for the whole breakdown. */
function UsageText({ u, byModel }: { u: Usage | null; byModel?: Record<string, number> }) {
  const models = useStore((s) => s.models);
  if (!u) {
    return (
      <span className="usage">
        <span className="k">Token</span> <span className="v">{UNKNOWN}</span>
      </span>
    );
  }
  return (
    <Tip text={<UsageTip u={u} byModel={byModel} models={models} />} className="usage" interactive>
      <span className="k">Token 合计</span>
      <span className="v">{formatCount(usageTotal(u))}</span>
      <span className="k">In</span>
      <span className="v">{formatCount(u.input)}</span>
      <span className="k">Out</span>
      <span className="v">{formatCount(u.output)}</span>
    </Tip>
  );
}

export function StatusBar() {
  const session = useStore(currentSession);
  const notice = useStore((s) => s.notice);
  return (
    <footer className="statusbar" role="status">
      <div className="st-left">
        {notice ? (
          <span key={notice.id} className="notice-text">
            {notice.text}
          </span>
        ) : session ? (
          <>
            <StateDot mark={session.mark} size={8} />
            <span>{session.mark === "attention" ? "等待你的确认" : session.workingLabel}</span>
          </>
        ) : (
          <span className="muted-text">没有打开的会话</span>
        )}
      </div>
      {session && (
        <div className="st-right">
          <ContextMeter c={session.context} />
          <span className="st-sep" aria-hidden />
          <SpeedText s={session.speed} />
          <span className="st-sep" aria-hidden />
          <UsageText u={session.usage} byModel={session.usageModels} />
        </div>
      )}
    </footer>
  );
}
