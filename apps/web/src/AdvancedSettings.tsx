import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { api, Json } from "./api";
import { Button, Field, RangeField, Sheet } from "./ui";
import {
  Draft, MAX_PATTERN_MINUTES, Pattern, Shape, Signal, SignalGroup, available, averageShare, baselineOf, groupNames, isVital,
  restoreDraft, runMinutes, scenario, signalInfo, signalNames, signalOrder, vitalScale,
} from "./workflow";
import { ABTestsTab, AppVersionsTab, JourneysTab, SpeedTab, TabIntro, VisitorsTab } from "./AdvancedTabs";
import { MiniCurve, minutesText } from "./SignalChart";

export type AdvancedTab = "Errors" | "Visitors" | "Speed" | "User journeys" | "A/B tests" | "App versions" | "Story";
const shapes: [Shape, string][] = [["steady", "Steady"], ["spike", "Spike"], ["rise", "Rise"], ["waves", "Waves"]];
const unavailable: Record<Signal, string> = {
  crashes: "Needs an Android or iOS app",
  freezes: "Needs an Android or iOS app",
  exceptions: "Needs an Android or web app",
  network: "",
  payments: "",
  lcp: "Needs a web app",
  inp: "Needs a web app",
  cls: "Needs a web app",
  fcp: "Needs a web app",
  ttfb: "Needs a web app",
};

/** The share readout for a signal: one value when steady, the range and average otherwise. */
export function shareText(k: Signal, d: Draft) {
  if (!d.signals[k]) return "Off";
  const p = d.patterns[k];
  if (p.shape === "steady") return `${d.rates[k]}%`;
  return `${Math.min(d.rates[k], baselineOf(k, p))}–${d.rates[k]}%`;
}

function SignalCard({ k, draft, set }: { k: Signal; draft: Draft; set: (d: Partial<Draft>) => void }) {
  const ok = available(k, draft), on = ok && draft.signals[k];
  const p = draft.patterns[k], info = signalInfo[k], name = signalNames[k], T = runMinutes(draft);
  const level = draft.signals[k] ? draft.rates[k] : 0;
  const pattern = (v: Partial<Pattern>) => set({ patterns: { ...draft.patterns, [k]: { ...p, ...v } } });
  const share = (v: number) =>
    set({
      signals: { ...draft.signals, [k]: v > 0 },
      rates: { ...draft.rates, [k]: v > 0 ? v : draft.rates[k] },
      patterns: { ...draft.patterns, [k]: { ...p, baseline: Math.min(p.baseline, v) } },
    });
  const maxWaves = Math.max(1, Math.min(8, Math.floor(T / 2)));
  const timed = T <= MAX_PATTERN_MINUTES;
  return (
    <section className={"signal-card" + (on ? "" : " off")} id={"signal-" + k} aria-labelledby={"signal-title-" + k}>
      <header>
        <i style={{ background: info.color }} aria-hidden="true" />
        <div>
          <h3 id={"signal-title-" + k}>{name}</h3>
          <p>{ok ? info.short : unavailable[k]}</p>
        </div>
        <strong className="signal-value">{ok ? shareText(k, draft) : "N/A"}</strong>
      </header>
      {ok && (
        <>
          <RangeField id={"signal-range-" + k} label={name + " share of sessions"} min={0} max={info.max} value={Math.min(level, info.max)}
            onChange={share} readout={level ? (p.shape === "steady" ? `${level}% of sessions` : `Peak ${level}%`) : "Off"}
            valueText={level ? `${level}% of sessions` : "Off"} />
          <div className="segmented pattern-switch" role="group" aria-label={name + " pattern"}>
            {shapes.map(([shape, label]) => (
              <button key={shape} type="button" aria-pressed={p.shape === shape} disabled={!on || (!timed && shape !== "steady")} onClick={() => pattern(p.shape === "steady" && shape !== "steady" && !p.baseline ? { shape, baseline: Math.round(level / 5) } : { shape })}>
                {label}
              </button>
            ))}
          </div>
          {on && !timed && p.shape !== "steady" && <p className="hint">Patterns apply to runs of up to 24 hours; this run sends a steady share.</p>}
          {on && timed && p.shape !== "steady" && (
            <div className="pattern-fields">
              {p.shape === "spike" && (
                <>
                  <RangeField label={name + " peak time"} min={0} max={100} step={5} value={p.peakAt} onChange={(v) => pattern({ peakAt: v })}
                    readout={"Peak at " + minutesText((p.peakAt / 100) * T)} valueText={minutesText((p.peakAt / 100) * T) + " into the run"} />
                  <RangeField label={name + " spike length"} min={10} max={100} step={5} value={p.width} onChange={(v) => pattern({ width: v })}
                    readout={"Lasts " + minutesText((p.width / 100) * T)} valueText={minutesText((p.width / 100) * T)} />
                </>
              )}
              {p.shape === "rise" && (
                <>
                  <RangeField label={name + " rise start"} min={0} max={95} step={5} value={Math.min(95, p.peakAt)} onChange={(v) => pattern({ peakAt: v })}
                    readout={"Starts at " + minutesText((Math.min(95, p.peakAt) / 100) * T)} valueText={minutesText((Math.min(95, p.peakAt) / 100) * T) + " into the run"} />
                  <RangeField label={name + " rise length"} min={5} max={100} step={5} value={p.width} onChange={(v) => pattern({ width: v })}
                    readout={"Climbs over " + minutesText((p.width / 100) * T)} valueText={minutesText((p.width / 100) * T)} />
                </>
              )}
              {p.shape === "waves" && (
                <RangeField label={name + " wave count"} min={1} max={maxWaves} value={Math.min(maxWaves, p.waves)} onChange={(v) => pattern({ waves: v })}
                  readout={`${Math.min(maxWaves, p.waves)} per run`} valueText={`${Math.min(maxWaves, p.waves)} waves`} />
              )}
              {k !== "payments" && (
                <RangeField label={name + " baseline"} min={0} max={level} value={Math.min(level, p.baseline)} onChange={(v) => pattern({ baseline: v })}
                  readout={`Baseline ${Math.min(level, p.baseline)}%`} valueText={`${Math.min(level, p.baseline)}% between peaks`} />
              )}
            </div>
          )}
          {isVital(k) && on && (() => {
            const r = vitalScale[k], v = Math.min(r.max, Math.max(r.min, draft.severity[k]));
            return (
              <RangeField label={name + " p75 on regressed pages"} min={r.min} max={r.max} step={r.step} value={v}
                onChange={(x) => set({ severity: { ...draft.severity, [k]: x } })} readout={`${name} p75 ${r.text(v)}`}
                valueText={r.unit ? `${v} ${r.unit}` : String(v)} />
            );
          })()}
          {on && (
            <div className="signal-curve">
              <MiniCurve signal={k} draft={draft} />
              <span>Start</span>
              <span>{p.shape === "steady" ? "" : `avg ${averageShare(k, draft).toFixed(1)}%`}</span>
              <span>{minutesText(T)}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Tabs({ tabs, tab, setTab }: { tabs: AdvancedTab[]; tab: AdvancedTab; setTab: (t: AdvancedTab) => void }) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const key = (e: KeyboardEvent) => {
    const i = tabs.indexOf(tab);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    const t = tabs[(next + tabs.length) % tabs.length];
    setTab(t);
    refs.current[t]?.focus();
  };
  return (
    <div className="sheet-tabs" role="tablist" aria-label="Advanced settings sections" onKeyDown={key}>
      {tabs.map((t) => (
        <button key={t} ref={(el) => (refs.current[t] = el)} role="tab" id={"tab-" + t} aria-selected={t === tab}
          aria-controls={"panel-" + t} tabIndex={t === tab ? 0 : -1} type="button" onClick={() => setTab(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}

export default function AdvancedSettings({
  open, onOpenChange, tab, setTab, focus, draft, update, caps, stories,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  tab: AdvancedTab;
  setTab: (t: AdvancedTab) => void;
  focus?: Signal;
  draft: Draft;
  update: (d: Partial<Draft>) => void;
  caps: Json;
  stories: { id: string; name: string }[];
}) {
  const [index, setIndex] = useState(0), [message, setMessage] = useState("");
  const file = useRef<HTMLInputElement>(null), meta = caps.modeling;
  const causal = draft.story === "causal";
  const tabs: AdvancedTab[] = causal ? ["Story"] : ["Errors", "Visitors", "Speed", "User journeys", "A/B tests", "App versions"];
  const current = tabs.includes(tab) ? tab : tabs[0];
  const custom = (d: Partial<Draft>) => update({ ...d, profile: "custom" });
  useEffect(() => {
    if (!open || !focus || current !== "Errors") return;
    const t = setTimeout(() => {
      document.getElementById("signal-" + focus)?.scrollIntoView({ block: "nearest" });
      document.getElementById("signal-range-" + focus)?.focus();
    }, 50);
    return () => clearTimeout(t);
  }, [open, focus, current]);
  const footer = (
    <>
      <div className="configuration-actions">
        <Button kind="quiet" onClick={() => {
          const url = URL.createObjectURL(new Blob([JSON.stringify({ format: "vantra-workflow", version: 1, draft }, null, 2)], { type: "application/json" }));
          const a = document.createElement("a");
          a.href = url; a.download = "vantra-configuration.json"; a.click(); URL.revokeObjectURL(url);
        }} aria-label="Save configuration"><span>Save<span className="wide-only"> configuration</span></span></Button>
        <Button kind="quiet" onClick={() => file.current?.click()} aria-label="Open configuration"><span>Open<span className="wide-only"> configuration</span></span></Button>
        <input ref={file} hidden type="file" accept="application/json,.json" onChange={async (e) => {
          const input = e.target;
          try {
            const f = input.files?.[0];
            if (!f) return;
            if (f.size > 1048576) throw new Error("Configuration exceeds 1 MiB.");
            const value = JSON.parse(await f.text());
            if (value.format !== "vantra-workflow" || value.version !== 1 || !value.draft) throw new Error("Choose an exported Vantra configuration.");
            const d = restoreDraft(value.draft);
            const checked = await api("/specs/validate", scenario(caps, d));
            if (!checked.valid) throw new Error(checked.issues.map((x: Json) => x.message).join(" "));
            update(d);
            setMessage("Configuration opened.");
          } catch (err) {
            setMessage((err as Error).message);
          } finally {
            input.value = "";
          }
        }} />
        {message && <p role="status" className="hint">{message}</p>}
      </div>
      <Button kind="primary" onClick={() => onOpenChange(false)}>Done</Button>
    </>
  );
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title="Advanced settings" description="Optional. Defaults are ready to send." footer={footer}>
      <Tabs tabs={tabs} tab={current} setTab={setTab} />
      <div role="tabpanel" id={"panel-" + current} aria-labelledby={"tab-" + current} className="sheet-panel">
        {current === "Errors" && (
          <>
            <TabIntro purpose="Which problems sessions contain, and when." effect="Changes crash, exception, network failure and Web Vitals counts in XCOR RUM." />
            {(["errors", "vitals"] as SignalGroup[]).map((group) => (
              <section key={group} className="signal-group" aria-labelledby={"group-" + group}>
                <h3 id={"group-" + group}>{groupNames[group]}</h3>
                {group === "vitals" && <p className="hint">Each share adds regressed pages on top of natural page speed.</p>}
                <div className="signal-cards">
                  {signalOrder.filter((k) => signalInfo[k].group === group).map((k) => <SignalCard key={k} k={k} draft={draft} set={custom} />)}
                </div>
              </section>
            ))}
          </>
        )}
        {current === "Visitors" && <VisitorsTab draft={draft} update={update} />}
        {current === "Speed" && meta && <SpeedTab draft={draft} update={update} caps={caps} index={index} setIndex={setIndex} />}
        {current === "User journeys" && <JourneysTab draft={draft} update={update} caps={caps} />}
        {current === "A/B tests" && <ABTestsTab draft={draft} update={update} />}
        {current === "App versions" && <AppVersionsTab draft={draft} update={update} caps={caps} />}
        {current === "Story" && causal && (
          <div className="field-grid">
            <div className="full"><TabIntro purpose="Fine-tune the selected story." effect="Defaults reproduce the documented story; change these only to vary it." /></div>
            <Field label="Traffic mix" hint="Only the story, or mixed with healthy, slow and recovered sessions.">
              <select value={draft.causalMode} onChange={(e) => update({ causalMode: e.target.value as Draft["causalMode"] })}>
                <option value="single">Selected story only</option><option value="mixed">Mixed traffic</option>
              </select>
            </Field>
            <Field label="Cache" hint="Cold adds cache misses on the first account load.">
              <select value={draft.causalCache} onChange={(e) => update({ causalCache: e.target.value as Draft["causalCache"] })}>
                <option value="cold">Cold account cache</option><option value="warm">Warm account cache</option>
              </select>
            </Field>
            <Field label="Network" hint="The connection the story's sessions use.">
              <select value={draft.causalNetwork} onChange={(e) => update({ causalNetwork: e.target.value as Draft["causalNetwork"] })}>
                <option value="wifi">Wi-Fi</option><option value="cellular">Cellular</option><option value="constrained">Constrained</option>
              </select>
            </Field>
            <Field label="Seed" hint="The same seed reproduces the same sessions."><input type="number" value={draft.causalSeed} onChange={(e) => update({ causalSeed: Number(e.target.value) })} /></Field>
            {draft.causalScenario === "contract-regression" && (
              <Field label="Client failure">
                <select value={draft.causalFailure} onChange={(e) => update({ causalFailure: e.target.value as Draft["causalFailure"] })}>
                  <option value="handled">Handled decoding error</option><option value="fatal">Unsafe native mapper · fatal</option>
                </select>
              </Field>
            )}
            {draft.causalScenario === "connectivity" && (
              <Field label="Failure boundary">
                <select value={draft.causalConnectivity} onChange={(e) => update({ causalConnectivity: e.target.value as Draft["causalConnectivity"] })}>
                  <option value="pre_send">Before reaching server</option><option value="post_send">After server response</option>
                </select>
              </Field>
            )}
            <Field label="Backend region">
              <select value={draft.causalRegion} onChange={(e) => update({ causalRegion: e.target.value })}>
                <option>us-east-1</option><option>eu-west-1</option><option>ap-southeast-2</option>
              </select>
            </Field>
            <Field label="Affected service release"><input value={draft.causalRelease} onChange={(e) => update({ causalRelease: e.target.value })} /></Field>
            <Field label="Timing" hint="Exact timings reproduce the documented reference run.">
              <select value={String(draft.causalReference)} onChange={(e) => update({ causalReference: e.target.value === "true" })}>
                <option value="false">Seeded variation</option><option value="true">Exact reference timings</option>
              </select>
            </Field>
            {draft.apps.some((a) => a.platform === "web") && (
              <Field label="Optional web provider">
                <select value={draft.causalProvider} onChange={(e) => update({ causalProvider: e.target.value })}>
                  <option value="none">None</option><option value="analytics">Consented analytics</option><option value="support">Opened support widget</option>
                </select>
              </Field>
            )}
            {draft.causalScenario === "frontend-regression" && draft.apps.every((a) => a.platform === "web") && (
              <Field label="Frontend variant">
                <select value={String(draft.causalWidget)} onChange={(e) => update({ causalWidget: e.target.value === "true" })}>
                  <option value="false">View-state failure</option><option value="true">Blocking support interaction</option>
                </select>
              </Field>
            )}

            {draft.causalMode === "mixed" && ["Healthy", "Selected story", "Slow dependency", "Recovered"].map((name, i) => (
              <Field key={name} label={name + " weight"}>
                <input type="number" min="0" max="100" value={draft.causalMix[i]} onChange={(e) => update({ causalMix: draft.causalMix.map((n, j) => (i === j ? Number(e.target.value) : n)) })} />
              </Field>
            ))}
            <p className="hint full">{stories.find((x) => x.id === draft.causalScenario)?.name} · {draft.causalVariant}</p>
          </div>
        )}
      </div>
    </Sheet>
  );
}
