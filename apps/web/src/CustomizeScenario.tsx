import { useState } from "react";
import { MixerHorizontalIcon } from "@radix-ui/react-icons";
import { Json } from "./api";
import { Badge, Button, Field, Panel, RangeField } from "./ui";
import { Draft, Signal, SignalGroup, available, flowNames, groupNames, isVital, profileMix, profiles, signalInfo, signalNames, signalOrder, vitalScale } from "./workflow";
import AdvancedSettings, { AdvancedTab, shareText } from "./AdvancedSettings";
import { ProfileGlyph, RunTimeline } from "./SignalChart";
import { quantityName } from "./AdvancedTabs";

export type Story = { id: string; name: string; line: string; domain: "Banking" | "Travel"; where: string };
/** What each connected story shows when it fails, and where the fault sits. */
export const stories: Story[] = [
  { id: "healthy", name: "Healthy account review", line: "Sign-in, account overview and history all succeed.", domain: "Banking", where: "No fault" },
  { id: "signin-outage", name: "Sign-in pool timeout", line: "Sign-in returns 503 when the session database pool is exhausted.", domain: "Banking", where: "Backend" },
  { id: "slow-history", name: "Slow transaction history", line: "History takes about 2 s because the query scans instead of using its index.", domain: "Banking", where: "Database" },
  { id: "contract-regression", name: "Transaction detail contract", line: "A changed detail response breaks the app's decoder: a handled error or a crash.", domain: "Banking", where: "API contract" },
  { id: "transfer-response-loss", name: "Transfer response lost", line: "The transfer succeeds but its response times out; the app confirms by polling.", domain: "Banking", where: "Network" },
  { id: "worker-backlog", name: "Transfer worker backlog", line: "The transfer is accepted, but a lagging worker keeps its status pending.", domain: "Banking", where: "Async worker" },
  { id: "frontend-regression", name: "Client view-state failure", line: "The transfer form fails to render in the app; the backend is healthy.", domain: "Banking", where: "Client" },
  { id: "connectivity", name: "Connectivity transition", line: "The device loses its connection while transaction history loads.", domain: "Banking", where: "Network" },
  { id: "travel-provider", name: "Travel provider degradation", line: "Hotel search: one supplier times out, so results come back partial.", domain: "Travel", where: "Third party" },
];
export const storyName = (id: string) => stories.find((x) => x.id === id)?.name || id;
const cohorts: [Draft["causalVariant"], string, string][] = [
  ["affected", "Affected", "Every session shows the failure."],
  ["control", "Healthy control", "The same journey without the failure, for comparison."],
  ["recovered", "Recovered", "The same journey after the fix, completing normally."],
  ["scheduled", "Scheduled incident", "Healthy before the window, failing during it, recovered after."],
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
  const groups = (["errors", "vitals"] as SignalGroup[])
    .map((group) => ({ group, keys: enabled.filter((k) => signalInfo[k].group === group) }))
    .filter((g) => g.keys.length);
  const extras = [
    ...(model?.bindings || []).map((b: Json) => (b.id === NETWORK_BINDING ? "Slow network" : quantityName(b.quantity) + " change")),
    ...(model?.experiments?.length ? [`${model.experiments.length} A/B test${model.experiments.length > 1 ? "s" : ""}`] : []),
    ...(model?.releases?.length ? ["App version rollout"] : []),
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
          <div className="story-setup">
            <fieldset className="story-grid">
              <legend>Story</legend>
              {stories.map((x) => (
                <label key={x.id} className="story-card">
                  <input type="radio" name="story" value={x.id} checked={draft.causalScenario === x.id} onChange={() => update({ causalScenario: x.id })} />
                  <strong>{x.name}</strong>
                  <span>{x.line}</span>
                  <small>{x.domain} · {x.where}</small>
                </label>
              ))}
            </fieldset>
            <fieldset className="chip-group cohort-group">
              <legend>Cohort</legend>
              {cohorts.map(([k, label]) => (
                <label key={k} className="chip">
                  <input type="radio" name="cohort" value={k} checked={draft.causalVariant === k} onChange={() => update({ causalVariant: k })} />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>
            <p className="hint" aria-live="polite">{cohorts.find(([k]) => k === draft.causalVariant)?.[2]}</p>
            {draft.causalVariant === "scheduled" && (
              <div className="field-grid">
                <Field label="Incident starts"><input type="datetime-local" value={draft.causalStart} onChange={(e) => update({ causalStart: e.target.value })} /></Field>
                <Field label="Incident minutes"><input type="number" min="1" max="120" value={draft.causalMinutes} onChange={(e) => update({ causalMinutes: Number(e.target.value) })} /></Field>
                <RangeField label="Affected cohort" min={0} max={100} value={draft.causalShare} onChange={(v) => update({ causalShare: v })}
                  readout={`${draft.causalShare}% of sessions in the window`} valueText={`${draft.causalShare}%`} />
              </div>
            )}
          </div>
        )}
      </Panel>
      {!causal && (
        <Panel title="Signal mix" className="mix-panel"
          action={<>{draft.profile === "custom" && <Badge>Custom mix</Badge>}<Button kind="quiet" onClick={() => show("Errors")}>Adjust signals</Button></>}>
          {groups.length ? groups.map(({ group, keys }) => (
            <section key={group} className="mix-group" aria-labelledby={"mix-" + group}>
              <h3 id={"mix-" + group}>{groupNames[group]}</h3>
              <RunTimeline draft={draft} keys={keys} title={groupNames[group]} />
              <ul className="mix-legend" aria-label={groupNames[group]}>
                {keys.map((k) => (
                  <li key={k}>
                    <button type="button" onClick={() => show("Errors", k)} aria-label={`Adjust ${signalNames[k]}: ${shareText(k, draft)} of sessions, ${shapeName[draft.patterns[k].shape].toLowerCase()}`}>
                      <i style={{ background: signalInfo[k].color }} aria-hidden="true" />
                      <span>{signalNames[k]}</span>
                      <small>{shapeName[draft.patterns[k].shape]}{isVital(k) ? " · p75 " + vitalScale[k].text(draft.severity[k]) : ""}</small>
                      <strong>{shareText(k, draft)}</strong>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )) : <p className="hint">No error signals. Sessions carry normal activity only.</p>}
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
