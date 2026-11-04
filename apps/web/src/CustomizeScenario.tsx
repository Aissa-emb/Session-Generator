import { useState } from "react";
import { MixerHorizontalIcon } from "@radix-ui/react-icons";
import { Json } from "./api";
import { Badge, Button, Field, Panel } from "./ui";
import { Draft, Signal, available, flowNames, profileMix, profiles, signalInfo, signalNames, signalOrder } from "./workflow";
import AdvancedSettings, { AdvancedTab, shareText } from "./AdvancedSettings";
import { ProfileGlyph, RunTimeline } from "./SignalChart";

export const stories = [
  ["healthy", "Healthy account review"], ["signin-outage", "Sign-in pool timeout"], ["slow-history", "Slow transaction history"],
  ["contract-regression", "Transaction detail contract"], ["transfer-response-loss", "Transfer response lost"],
  ["worker-backlog", "Transfer worker backlog"], ["frontend-regression", "Client view-state failure"],
  ["connectivity", "Connectivity transition"], ["travel-provider", "Travel provider degradation"],
];
const shapeName = { steady: "Steady", spike: "Spike", rise: "Rise", waves: "Waves" };
const NETWORK_BINDING = "scenario-network";

export default function CustomizeScenario({ draft, update, caps }: { draft: Draft; update: (d: Partial<Draft>) => void; caps: Json }) {
  const [open, setOpen] = useState(false), [tab, setTab] = useState<AdvancedTab>("Errors"), [focus, setFocus] = useState<Signal>();
  const causal = draft.story === "causal";
  const model: Json | undefined = draft.modeling;
  const show = (t: AdvancedTab, k?: Signal) => {
    setTab(t);
    setFocus(k);
    setOpen(true);
  };
  /** Flaky network adds constrained request transit; other profiles remove only that addition. */
  function modelingFor(id: string): Partial<Draft> {
    const kept = (model?.bindings || []).filter((b: Json) => b.id !== NETWORK_BINDING);
    if (id === "network" && caps.modeling) {
      const distribution = structuredClone(caps.modeling.defaults.network_rtt_ms);
      distribution.parameters = { mu: Math.log(150), sigma: 0.65 };
      return { modelingPreset: "network-profile", modeling: { version: "1.0", experiments: [], ...model,
        bindings: [...kept, { id: NETWORK_BINDING, quantity: "network_rtt_ms", distribution, share: 1 }] } };
    }
    if (!model) return {};
    const empty = !kept.length && !model.experiments?.length && !model.releases?.length;
    return empty ? { modeling: undefined, modelingPreset: undefined } : { modeling: { ...model, bindings: kept } };
  }
  function choose(id: string) {
    if (id === "story") update({ profile: "story", story: "causal" });
    else update({ ...profileMix(id), ...modelingFor(id), profile: id, story: "standard" });
  }
  const enabled = signalOrder.filter((k) => draft.signals[k] && available(k, draft));
  const off = signalOrder.filter((k) => available(k, draft) && !draft.signals[k]);
  const extras = [
    ...(model?.bindings || []).map((b: Json) => (b.id === NETWORK_BINDING ? "Constrained network" : caps.modeling?.quantities[b.quantity]?.name + " override")),
    ...(model?.experiments?.length ? [`${model.experiments.length} experiment${model.experiments.length > 1 ? "s" : ""}`] : []),
    ...(model?.releases?.length ? ["Release adoption curve"] : []),
  ];
  return (
    <>
      <Panel title="Data profile" className="profile-panel"
        action={<Button kind="quiet" onClick={() => show(causal ? "Story" : tab === "Story" ? "Errors" : tab)}><MixerHorizontalIcon aria-hidden="true" />Advanced settings</Button>}>
        {!causal && (
          <fieldset className="chip-group">
            <legend className="sr-only">User flow</legend>
            {Object.entries(flowNames).map(([k, v]) => (
              <label key={k} className="chip">
                <input type="radio" name="flow" value={k} checked={draft.flow === k} onChange={() => update({ flow: k })} />
                <span>{v}</span>
              </label>
            ))}
          </fieldset>
        )}
        <fieldset className="profile-grid">
          <legend className="sr-only">Error profile</legend>
          {profiles.map((p) => (
            <label key={p.id} className="profile-card">
              <input type="radio" name="profile" value={p.id} checked={draft.profile === p.id} onChange={() => choose(p.id)} />
              <ProfileGlyph id={p.id} />
              <strong>{p.name}</strong>
              <span>{p.line}</span>
            </label>
          ))}
        </fieldset>
        {causal && (
          <div className="field-grid">
            <Field label="Story">
              <select value={draft.causalScenario} onChange={(e) => update({ causalScenario: e.target.value })}>
                {stories.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </Field>
            <Field label="Cohort">
              <select value={draft.causalVariant} onChange={(e) => update({ causalVariant: e.target.value as Draft["causalVariant"] })}>
                <option value="affected">Affected</option><option value="control">Healthy control</option>
                <option value="recovered">Recovered</option><option value="scheduled">Scheduled incident</option>
              </select>
            </Field>
          </div>
        )}
      </Panel>
      {!causal && (
        <Panel title="Signal mix" className="mix-panel"
          action={<>{draft.profile === "custom" && <Badge>Custom mix</Badge>}<Button kind="quiet" onClick={() => show("Errors")}>Adjust signals</Button></>}>
          {enabled.length ? <RunTimeline draft={draft} keys={enabled} /> : <p className="hint">No error signals. Sessions carry normal activity only.</p>}
          <ul className="mix-legend" aria-label="Signals">
            {enabled.map((k) => (
              <li key={k}>
                <button type="button" onClick={() => show("Errors", k)} aria-label={`Adjust ${signalNames[k]}: ${shareText(k, draft)} of sessions, ${shapeName[draft.patterns[k].shape].toLowerCase()}`}>
                  <i style={{ background: signalInfo[k].color }} aria-hidden="true" />
                  <span>{signalNames[k]}</span>
                  <small>{shapeName[draft.patterns[k].shape]}</small>
                  <strong>{shareText(k, draft)}</strong>
                </button>
              </li>
            ))}
          </ul>
          {(off.length > 0 || extras.length > 0) && (
            <p className="hint mix-notes">
              {off.length > 0 && <span>Off: {off.map((k) => signalNames[k]).join(", ")}</span>}
              {extras.length > 0 && <span>Also: {extras.join(" · ")}</span>}
            </p>
          )}
        </Panel>
      )}
      <AdvancedSettings open={open} onOpenChange={setOpen} tab={tab} setTab={setTab} focus={focus} draft={draft} update={update} caps={caps} stories={stories} />
    </>
  );
}
