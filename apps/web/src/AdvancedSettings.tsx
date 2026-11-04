import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { api, Json } from "./api";
import { Button, Field, RangeField, Sheet } from "./ui";
import {
  Draft, MAX_PATTERN_MINUTES, Pattern, Shape, Signal, available, averageShare, baselineOf, restoreDraft, runMinutes, scenario,
  signalInfo, signalNames, signalOrder,
} from "./workflow";
import DistributionEditor from "./DistributionEditor";
import ReleaseEditor from "./ReleaseEditor";
import { MiniCurve, minutesText } from "./SignalChart";

export type AdvancedTab = "Errors" | "Population" | "Performance" | "Journey" | "Experiments" | "Release" | "Story";
const shapes: [Shape, string][] = [["steady", "Steady"], ["spike", "Spike"], ["rise", "Rise"], ["waves", "Waves"]];
const unavailable: Record<Signal, string> = {
  crashes: "Needs an Android or iOS app",
  freezes: "Needs an Android or iOS app",
  exceptions: "Needs an Android or web app",
  pages: "Needs a web app",
  network: "",
  payments: "",
};

function Weights({ values, change, label }: { values: Json; change: (v: Json) => void; label: string }) {
  const total = Object.values(values).reduce((a: number, b) => a + Number(b), 0);
  return (
    <fieldset className="weight-editor">
      <legend>{label}</legend>
      <p className="hint">Relative weights · normalized to 100%.</p>
      {Object.entries(values).map(([key, value]) => (
        <label key={key}>
          <span>{key.replaceAll("_", " ")}</span>
          <input aria-label={`${label}: ${key}`} type="number" min="0" value={value as number}
            onChange={(e) => change({ ...values, [key]: Number(e.target.value) })} />
          <small>{total ? Math.round((Number(value) / total) * 100) : 0}%</small>
        </label>
      ))}
    </fieldset>
  );
}

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
          {k === "pages" && on && (
            <RangeField label="Slow page LCP p75" min={2500} max={15000} step={100} value={Math.min(15000, draft.lcp)}
              onChange={(v) => set({ lcp: v })} readout={`LCP p75 ${(draft.lcp / 1000).toFixed(1)} s`} valueText={`${draft.lcp} milliseconds`} />
          )}
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
  stories: string[][];
}) {
  const [index, setIndex] = useState(0), [quantity, setQuantity] = useState("reading_ms"), [message, setMessage] = useState("");
  const file = useRef<HTMLInputElement>(null), meta = caps.modeling;
  const causal = draft.story === "causal";
  const tabs: AdvancedTab[] = causal ? ["Story"] : ["Errors", "Population", "Performance", "Journey", "Experiments", "Release"];
  const current = tabs.includes(tab) ? tab : tabs[0];
  const model = draft.modeling || { version: "1.0", bindings: [], experiments: [] };
  const bindings: Json[] = model.bindings || [], active = bindings[Math.min(index, bindings.length - 1)];
  const setModel = (v: Json) => update({ modeling: v, modelingPreset: "custom" });
  const patch = (b: Json) => setModel({ ...model, bindings: bindings.map((v, i) => (i === Math.min(index, bindings.length - 1) ? b : v)) });
  const custom = (d: Partial<Draft>) => update({ ...d, profile: "custom" });
  useEffect(() => {
    if (!open || !focus || current !== "Errors") return;
    const t = setTimeout(() => {
      document.getElementById("signal-" + focus)?.scrollIntoView({ block: "nearest" });
      document.getElementById("signal-range-" + focus)?.focus();
    }, 50);
    return () => clearTimeout(t);
  }, [open, focus, current]);
  function performance(id: "tail" | "network-profile") {
    const q = id === "tail" ? "backend_work_ms" : "network_rtt_ms", d = structuredClone(meta.defaults[q]);
    d.parameters = { mu: Math.log(id === "tail" ? 50 : 150), sigma: id === "tail" ? 0.8 : 0.65 };
    update({ modelingPreset: id, modeling: { ...model, bindings: [...bindings, { id: "scenario-" + (id === "tail" ? "tail" : "network"), quantity: q,
      distribution: d, share: id === "tail" ? 0.5 : 1, ...(id === "tail" ? { effect: { kind: "tail", factor: 4, knee: 0.8 } } : {}) }] } });
    setIndex(bindings.length);
  }
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
          <div className="signal-cards">
            {signalOrder.map((k) => <SignalCard key={k} k={k} draft={draft} set={custom} />)}
          </div>
        )}
        {current === "Population" && (
          <>
            <div className="field-grid">
              <Field label="Visit patterns">
                <select value={draft.population} onChange={(e) => update({ population: e.target.value as Draft["population"] })}>
                  <option value="varied">Varied visits</option>
                  <option value="legacy">Previous distribution</option>
                </select>
              </Field>
            </div>
            <div className="range-stack">
              <RangeField label="Returning visitors" min={0} max={100} value={draft.returning} onChange={(v) => update({ returning: v })}
                readout={`${draft.returning}%`} valueText={`${draft.returning}%`} />
              {draft.apps.some((a) => a.platform !== "web") && (
                <>
                  <RangeField label="Cold starts on return" min={0} max={100} value={draft.coldStarts} onChange={(v) => update({ coldStarts: v })}
                    readout={`${draft.coldStarts}%`} valueText={`${draft.coldStarts}%`} />
                  <RangeField label="App interruptions" min={0} max={100} value={draft.interruptions} onChange={(v) => update({ interruptions: v })}
                    readout={`${draft.interruptions}%`} valueText={`${draft.interruptions}%`} />
                </>
              )}
            </div>
            <Button onClick={() => update({ populationOverrides: { ...draft.populationOverrides, network_mix: draft.populationOverrides?.network_mix ? undefined : { wifi: 45, "fast-4g": 25, "slow-4g": 20, "fast-3g": 8, "slow-3g": 2 } } })}>
              {draft.populationOverrides?.network_mix ? "Use device-based network defaults" : "Customize network weights"}
            </Button>
            {draft.populationOverrides?.network_mix && (
              <Weights label="Network weights" values={draft.populationOverrides.network_mix}
                change={(v) => update({ populationOverrides: { ...draft.populationOverrides, network_mix: v } })} />
            )}
          </>
        )}
        {current === "Journey" && (
          <>
            <p className="hint">Intent weights guide real paths. A purchase still requires successful preceding operations.</p>
            {Object.entries(caps.population_model || {}).filter(([lob]) => draft.flow === "all" || draft.flow === lob).map(([lob, values]) => (
              <Weights key={lob} label={lob + " intent weights"} values={draft.populationOverrides?.intent_mix?.[lob] || (values as Json)}
                change={(v) => update({ population: "varied", populationOverrides: { ...draft.populationOverrides, intent_mix: { ...draft.populationOverrides?.intent_mix, [lob]: v } } })} />
            ))}
          </>
        )}
        {current === "Performance" && meta && (
          <>
            <div className="quick-actions" role="group" aria-label="Performance presets">
              <Button onClick={() => performance("tail")}>Tail-only slowdown</Button>
              <Button onClick={() => performance("network-profile")}>Constrained network</Button>
            </div>
            <div className="editor-toolbar">
              <Field label="Quantity">
                <select value={quantity} onChange={(e) => setQuantity(e.target.value)}>
                  {Object.entries(meta.quantities).map(([k, v]) => <option key={k} value={k}>{(v as Json).name}</option>)}
                </select>
              </Field>
              <Button onClick={() => {
                setModel({ ...model, bindings: [...bindings, { id: quantity + "-" + Date.now().toString(36), quantity,
                  priority: Math.max(0, ...bindings.map((b) => b.priority || 0)) + 1, distribution: structuredClone(meta.defaults[quantity]) }] });
                setIndex(bindings.length);
              }}>Add override</Button>
            </div>
            {active ? (
              <>
                <Field label="Performance override">
                  <select value={Math.min(index, bindings.length - 1)} onChange={(e) => setIndex(Number(e.target.value))}>
                    {bindings.map((b, i) => <option key={b.id} value={i}>{meta.quantities[b.quantity]?.name} · {i + 1}</option>)}
                  </select>
                </Field>
                <DistributionEditor binding={active} metadata={meta} change={patch} />
                <details>
                  <summary>Audience and effect</summary>
                  <div className="field-grid">
                    <Field label="Audience platform">
                      <select value={active.cohort?.platforms?.[0] || ""} onChange={(e) => patch({ ...active, cohort: { ...active.cohort, platforms: e.target.value ? [e.target.value] : [] } })}>
                        <option value="">All selected platforms</option>
                        {draft.apps.map((a) => <option key={a.platform}>{a.platform}</option>)}
                      </select>
                    </Field>
                    <Field label="Audience device tier">
                      <select value={active.cohort?.device_tiers?.[0] || ""} onChange={(e) => patch({ ...active, cohort: { ...active.cohort, device_tiers: e.target.value ? [e.target.value] : [] } })}>
                        <option value="">All devices</option><option value="flagship">Flagship</option><option value="mid">Mid-range</option><option value="budget">Budget</option>
                      </select>
                    </Field>
                    {["networks", "intents", "app_versions", "screens", ...(["backend_work_ms", "network_rtt_ms"].includes(active.quantity) ? ["endpoints"] : [])].map((key) => (
                      <Field key={key} label={"Audience " + key.replaceAll("_", " ")} hint="Comma-separated; empty means all.">
                        <input value={(active.cohort?.[key] || []).join(", ")} onChange={(e) => patch({ ...active, cohort: { ...active.cohort, [key]: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) } })} />
                      </Field>
                    ))}
                    <Field label="Audience visits">
                      <select value={active.cohort?.returning === undefined ? "all" : String(active.cohort.returning)} onChange={(e) => {
                        const cohort = { ...active.cohort };
                        if (e.target.value === "all") delete cohort.returning; else cohort.returning = e.target.value === "true";
                        patch({ ...active, cohort });
                      }}>
                        <option value="all">New and returning</option><option value="true">Returning</option><option value="false">New</option>
                      </select>
                    </Field>
                    {(model.experiments || []).map((x: Json) => (
                      <Field key={x.id} label={"Exposed to " + x.id}>
                        <select value={active.cohort?.experiments?.[x.id]?.[0] || ""} onChange={(event) => {
                          const experiments = { ...(active.cohort?.experiments || {}) };
                          if (event.target.value) experiments[x.id] = [event.target.value]; else delete experiments[x.id];
                          patch({ ...active, cohort: { ...active.cohort, experiments } });
                        }}>
                          <option value="">Any assignment</option>
                          {Object.keys(x.variants).map((v) => <option key={v}>{v}</option>)}
                        </select>
                      </Field>
                    ))}
                    <Field label="Effect">
                      <select value={active.effect?.kind || "none"} onChange={(e) => {
                        const next = { ...active };
                        if (e.target.value === "none") delete next.effect; else next.effect = { kind: e.target.value, factor: 4, knee: 0.8 };
                        patch(next);
                      }}>
                        <option value="none">Baseline distribution</option>
                        <option value="scale">Scale the whole distribution</option>
                        <option value="tail">Worsen the tail only</option>
                      </select>
                    </Field>
                    {active.effect && (
                      <>
                        <Field label="Effect factor">
                          <input type="number" step=".1" min="1" max="20" value={active.effect.factor} onChange={(e) => patch({ ...active, effect: { ...active.effect, factor: Number(e.target.value) } })} />
                        </Field>
                        <Field label="Affected visits (%)">
                          <input type="number" min="0" max="100" value={(active.share ?? 1) * 100} onChange={(e) => patch({ ...active, share: Number(e.target.value) / 100 })} />
                        </Field>
                      </>
                    )}
                    {active.effect?.kind === "tail" && (
                      <Field label="Tail begins at percentile">
                        <input type="number" min="50" max="99" value={(active.effect.knee ?? 0.8) * 100} onChange={(e) => patch({ ...active, effect: { ...active.effect, knee: Number(e.target.value) / 100 } })} />
                      </Field>
                    )}
                    <Field label="Override priority" hint="Higher matching priority wins.">
                      <input type="number" min="0" max="1000" value={active.priority || 0} onChange={(e) => patch({ ...active, priority: Number(e.target.value) })} />
                    </Field>
                  </div>
                </details>
                <Button onClick={() => {
                  const rest = bindings.filter((b) => b !== active);
                  update({ modeling: { ...model, bindings: rest }, modelingPreset: rest.length || model.experiments?.length || model.releases?.length ? "custom" : undefined });
                }}>Remove override</Button>
              </>
            ) : (
              <p className="hint">Using device and operation defaults.</p>
            )}
          </>
        )}
        {current === "Release" && (
          <ReleaseEditor apps={draft.apps} catalog={caps.release_catalog} curves={model.releases || []} change={(releases) => setModel({ ...model, releases })} />
        )}
        {current === "Experiments" && (
          <>
            <p className="hint">Device-sticky assignment. Exposure starts at a matching request. Target variants under Performance → Audience.</p>
            {(model.experiments || []).map((x: Json, i: number) => {
              const change = (v: Json) => setModel({ ...model, experiments: model.experiments.map((y: Json, j: number) => (i === j ? { ...y, ...v } : y)) });
              return (
                <div className="experiment-editor" key={i}>
                  <div className="field-grid">
                    <Field label="Experiment ID"><input value={x.id} onChange={(event) => change({ id: event.target.value })} /></Field>
                    <Field label="Enrollment (%)"><input type="number" min="0" max="100" value={(x.enrollment ?? 1) * 100} onChange={(event) => change({ enrollment: Number(event.target.value) / 100 })} /></Field>
                    <Field label="Exposure endpoints" hint="For example get_product or add_to_cart."><input value={x.exposure_endpoints.join(", ")} onChange={(event) => change({ exposure_endpoints: event.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} /></Field>
                    <Field label="Assignment version"><input value={x.version || "1"} onChange={(event) => change({ version: event.target.value })} /></Field>
                  </div>
                  <Weights label={x.id + " variant weights"} values={x.variants} change={(v) => change({ variants: v })} />
                  <Button onClick={() => setModel({ ...model, experiments: model.experiments.filter((_: Json, j: number) => j !== i) })}>Remove experiment</Button>
                </div>
              );
            })}
            <Button onClick={() => setModel({ ...model, experiments: [...(model.experiments || []), { id: "feature-" + ((model.experiments || []).length + 1), version: "1", enrollment: 1, variants: { control: 50, treatment: 50 }, exposure_endpoints: ["get_product"] }] })}>Add experiment</Button>
          </>
        )}
        {current === "Story" && causal && (
          <div className="field-grid">
            <Field label="Traffic mix">
              <select value={draft.causalMode} onChange={(e) => update({ causalMode: e.target.value as Draft["causalMode"] })}>
                <option value="single">Selected story only</option><option value="mixed">Mixed traffic</option>
              </select>
            </Field>
            <Field label="Cache">
              <select value={draft.causalCache} onChange={(e) => update({ causalCache: e.target.value as Draft["causalCache"] })}>
                <option value="cold">Cold account cache</option><option value="warm">Warm account cache</option>
              </select>
            </Field>
            <Field label="Network">
              <select value={draft.causalNetwork} onChange={(e) => update({ causalNetwork: e.target.value as Draft["causalNetwork"] })}>
                <option value="wifi">Wi-Fi</option><option value="cellular">Cellular</option><option value="constrained">Constrained</option>
              </select>
            </Field>
            <Field label="Seed"><input type="number" value={draft.causalSeed} onChange={(e) => update({ causalSeed: Number(e.target.value) })} /></Field>
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
            <Field label="Timing">
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
            {draft.causalVariant === "scheduled" && (
              <>
                <Field label="Incident starts"><input type="datetime-local" value={draft.causalStart} onChange={(e) => update({ causalStart: e.target.value })} /></Field>
                <Field label="Incident minutes"><input type="number" min="1" max="120" value={draft.causalMinutes} onChange={(e) => update({ causalMinutes: Number(e.target.value) })} /></Field>
                <Field label="Affected cohort %"><input type="number" min="0" max="100" value={draft.causalShare} onChange={(e) => update({ causalShare: Number(e.target.value) })} /></Field>
              </>
            )}
            {draft.causalMode === "mixed" && ["Healthy", "Selected story", "Slow dependency", "Recovered"].map((name, i) => (
              <Field key={name} label={name + " weight"}>
                <input type="number" min="0" max="100" value={draft.causalMix[i]} onChange={(e) => update({ causalMix: draft.causalMix.map((n, j) => (i === j ? Number(e.target.value) : n)) })} />
              </Field>
            ))}
            <p className="hint full">{stories.find(([k]) => k === draft.causalScenario)?.[1]} · {draft.causalVariant}</p>
          </div>
        )}
      </div>
    </Sheet>
  );
}
