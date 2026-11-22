import { useEffect, useMemo, useState } from "react";
import { LIVE } from "../mode";
import { liveCall } from "../liveBridge";
import { messageOf } from "../lib/errors";
import type { StorageCategoryView, StorageView } from "../protocol";
import { Field, Seg, Switch, TextInput } from "./formkit";

const GIB = 1024 ** 3;
const SHARE_SCALE = 1_000_000;
const COLORS: Record<string, string> = { history: "#9db8d9", dump: "#d9b78a", cache: "#9cc4a5", index: "#b3a4d9" };

export function formatBytes(n: number): string {
  if (n >= GIB) return `${(n / GIB).toFixed(1)} GiB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${n} B`;
}

/** One share input as a percent, or null when it is not a usable 0–100 number. */
export function shareValue(raw: string): number | null {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 && n <= 100 ? n : null;
}

/** The split may be saved only when it totals exactly 100 — same rule as the backend's Decimal check. */
export function sharesComplete(values: Array<number | null>): boolean {
  let total = 0;
  for (const v of values) if (v === null) return false; else total += Math.round(v * SHARE_SCALE);
  return total === 100 * SHARE_SCALE;
}

/** The running total for the hint line, in hundredths of a percent. */
export function sharesTotal(values: Array<number | null>): number {
  let total = 0;
  for (const v of values) total += v === null ? 0 : Math.round(v * 100);
  return total / 100;
}

const DEMO: StorageView = {
  maxBytes: 8 * GIB,
  alertAt: 80,
  fullPolicy: "stop",
  shares: { history: 60, dump: 15, cache: 15, index: 10 },
  autoclean: ["cache", "index"],
  categories: [
    { id: "history", label: "完整历史", share: 60, budget: 4.8 * GIB, used: null, known: false, over: false },
    { id: "dump", label: "转储", share: 15, budget: 1.2 * GIB, used: null, known: false, over: false },
    { id: "cache", label: "缓存", share: 15, budget: 1.2 * GIB, used: 40 * 1024 ** 2, known: true, over: false },
    { id: "index", label: "可重建索引", share: 10, budget: 0.8 * GIB, used: 6 * 1024 ** 2, known: true, over: false },
  ],
  total: { used: null, known: false, alerted: false },
};

/** The occupancy donut: real measured bytes only; a category the application does not measure is
 *  listed as "未统计" and never drawn as zero. */
function UsageChart({ view }: { view: StorageView }) {
  const known = view.categories.filter((c) => c.known && (c.used ?? 0) > 0);
  const measured = known.reduce((sum, c) => sum + (c.used ?? 0), 0);
  const R = 54;
  const C = 2 * Math.PI * R;
  let offset = 0;
  return (
    <div className="storage-usage">
      <svg viewBox="0 0 140 140" className="storage-donut" role="img" aria-label="实际占用">
        <circle cx="70" cy="70" r={R} fill="none" strokeWidth="18" className="storage-donut-track" />
        {known.map((c) => {
          const part = measured ? (c.used ?? 0) / measured : 0;
          const dash = `${part * C} ${C}`;
          const el = <circle key={c.id} cx="70" cy="70" r={R} fill="none" stroke={COLORS[c.id] ?? "#bbb"} strokeWidth="18" strokeDasharray={dash} strokeDashoffset={-offset * C} transform="rotate(-90 70 70)" />;
          offset += part;
          return el;
        })}
        <text x="70" y="66" textAnchor="middle" className="storage-donut-num">{measured ? formatBytes(measured) : "—"}</text>
        <text x="70" y="82" textAnchor="middle" className="storage-donut-cap">已统计</text>
      </svg>
      <div className="storage-legend">
        {view.categories.map((c: StorageCategoryView) => (
          <div key={c.id} className="storage-row">
            <span className="storage-swatch" style={{ background: c.known ? COLORS[c.id] ?? "#bbb" : "#cfcfcf" }} />
            <span className="storage-name">{c.label}</span>
            <span className="storage-used">{c.known ? formatBytes(c.used ?? 0) : "未统计"}</span>
            <span className="storage-budget">限额 {formatBytes(c.budget)}{c.over ? " · 已超" : ""}</span>
          </div>
        ))}
        <div className="storage-row storage-total">
          <span className="storage-name">合计</span>
          <span className="storage-used">
            {view.total.known ? formatBytes(view.total.used ?? 0) : `已统计 ${formatBytes(measured)}，部分未知`}
          </span>
          <span className="storage-budget">上限 {formatBytes(view.maxBytes)}{view.total.alerted ? " · 已超提醒线" : ""}</span>
        </div>
      </div>
    </div>
  );
}

export function StorageSettings() {
  const [view, setView] = useState<StorageView | null>(LIVE ? null : DEMO);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [gib, setGib] = useState("");
  const [alert, setAlert] = useState("");
  const [shares, setShares] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!LIVE) return;
    liveCall("storage.get", {}).then(setView).catch((e) => setError(messageOf(e)));
  }, []);
  useEffect(() => {
    if (!view) return;
    setGib((view.maxBytes / GIB).toFixed(view.maxBytes % GIB ? 2 : 0));
    setAlert(String(view.alertAt));
    setShares(Object.fromEntries(view.categories.map((c) => [c.id, String(c.share)])));
  }, [view]);

  const send = async (key: "maxBytes" | "shares" | "alertAt" | "fullPolicy" | "autoclean", value: number | string | string[] | Record<string, number>) => {
    setBusy(true); setError("");
    try { if (LIVE) setView(await liveCall("storage.set", { key, value })); }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  };

  const shareValues = useMemo(() => view?.categories.map((c) => shareValue(shares[c.id] ?? "")) ?? [], [shares, view]);
  const parsedShares = useMemo(() => {
    if (shareValues.some((v) => v === null)) return null;
    return Object.fromEntries((view?.categories ?? []).map((c, i) => [c.id, shareValues[i]])) as Record<string, number>;
  }, [shareValues, view]);
  const sharesOk = sharesComplete(shareValues);
  const sharesChanged = parsedShares !== null && (view?.categories ?? []).some((c) => parsedShares[c.id] !== c.share);

  if (!view) return <div className="field-hint">正在读取…</div>;
  const commitGib = () => {
    const n = Number(gib);
    if (Number.isFinite(n) && n * GIB === view.maxBytes) return;
    if (!Number.isFinite(n) || n <= 0) { setError("上限必须是正的 GiB 数。"); return; }
    void send("maxBytes", Math.round(n * GIB));
  };
  const commitAlert = () => {
    const n = Number(alert);
    if (Number.isInteger(n) && n === view.alertAt) return;
    if (!Number.isInteger(n) || n < 10 || n > 100) { setError("提醒阈值是 10 到 100 的整数百分比。"); return; }
    void send("alertAt", n);
  };

  return (
    <>
      <Field label="全局上限 Max" hint="本应用托管数据（不含引擎自己的会话文件）的总量上限；各分类按下方百分比固定分配。">
        <span className="storage-inline" onBlur={commitGib}>
          <TextInput label="全局上限 GiB" type="number" value={gib} width={90} onChange={setGib} onEnter={commitGib} />
          <span className="field-hint">GiB</span>
        </span>
      </Field>
      <Field label="分类占比" hint="各类限额 = 全局上限 × 占比，合计必须等于 100%；任何一类不能借用其他类未用的额度。">
        <div className="storage-shares">
          {view.categories.map((c) => (
            <label key={c.id} className="storage-share">
              <span>{c.label}</span>
              <TextInput label={`${c.label}占比`} type="number" value={shares[c.id] ?? ""} width={70} onChange={(v) => setShares((s) => ({ ...s, [c.id]: v }))} />
              <span className="field-hint">% → {formatBytes(Math.floor(view.maxBytes * (parsedShares?.[c.id] ?? 0) / 100))}</span>
            </label>
          ))}
          <div className="storage-share-sum">
            合计 <b className={sharesOk ? "" : "storage-bad"}>{sharesTotal(shareValues).toFixed(2)}%</b>
            <button type="button" className="btn" disabled={busy || !sharesOk || !sharesChanged} onClick={() => void send("shares", parsedShares ?? {})}>
              保存配比
            </button>
          </div>
        </div>
      </Field>
      <Field label="总占用提醒阈值" hint="总占用达到上限的这个百分比时提醒；与上面的配比无关。">
        <span className="storage-inline" onBlur={commitAlert}>
          <TextInput label="提醒阈值百分比" type="number" value={alert} width={70} onChange={setAlert} onEnter={commitAlert} />
          <span className="field-hint">%</span>
        </span>
      </Field>
      <Field label="满额时" hint="达到上限时新的写入会被拒绝。选「自动清理」时，在同一写入事务里先删下方已授权的最老合格对象再重试；没有合格对象或删完仍不够照样拒写——目前只有转储真正可清。">
        <Seg<"stop" | "autoclean">
          value={view.fullPolicy}
          onChange={(v) => void send("fullPolicy", v)}
          options={[{ id: "stop", label: "停止新增，手动处理" }, { id: "autoclean", label: "自动清理最老的合格对象" }]}
        />
      </Field>
      {view.fullPolicy === "autoclean" && (
        <Field label="允许自动清理" hint="只有转储的清理已实现：删最老的非最新已提交快照文件，manifest 代次记录保留为明确缺口、原始事件留在历史日志；其余分类勾选暂不生效，满额仍拒写。">
          <div className="storage-autoclean">
            {view.categories.map((c) => {
              const implemented = c.id === "dump";
              return (
                <label key={c.id} className="storage-autoclean-row">
                  <Switch
                    on={view.autoclean.includes(c.id)}
                    label={`自动清理${c.label}${implemented ? "" : "（未实现）"}`}
                    disabled={!implemented}
                    onChange={(v) => void send("autoclean", v ? [...view.autoclean, c.id] : view.autoclean.filter((x) => x !== c.id))}
                  />
                  <span>{c.label}{!implemented ? "（清理未实现）" : ""}{!c.known ? "（未接入）" : ""}</span>
                </label>
              );
            })}
          </div>
        </Field>
      )}
      {error && <div className="stack-err">{error}</div>}
      <Field label="实际占用" hint="只统计本应用维护的数据；标「未统计」的分类由对应模块接入后才有数字，不当作 0。">
        <UsageChart view={view} />
      </Field>
    </>
  );
}
