import { CSSProperties, ReactNode, useId } from "react";
import { Json } from "./api";
import { Button, Field, RangeField } from "./ui";
import { Draft, flowNames, runMinutes } from "./workflow";
import DistributionEditor from "./DistributionEditor";
import ReleaseEditor from "./ReleaseEditor";

/* ---------- shared pieces ---------- */

const PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];

/** One line on what a tab is for and what it changes in XCOR RUM. */
export function TabIntro({ purpose, effect }: { purpose: string; effect: string }) {
  return (
    <p className="tab-intro">
      <strong>{purpose}</strong> {effect}
    </p>
  );
}

/** Named quick choices shown first on a tab; each has a one-line result. */
export function QuickChoices({ label, options, value, onChange }: {
  label: string;
  options: { id: string; name: string; line: string }[];
  value: string;
  onChange: (id: string) => void;
}) {
  const name = useId();
  return (
    <fieldset className="quick-choices" style={{ "--columns": options.length === 4 ? 2 : options.length } as CSSProperties}>
      <legend className="sr-only">{label}</legend>
      {options.map((o) => (
        <label key={o.id} className="quick-choice">
          <input type="radio" name={name} value={o.id} checked={value === o.id} onChange={() => onChange(o.id)} />
          <strong>{o.name}</strong>
          <span>{o.line}</span>
        </label>
      ))}
      {value === "custom" && <p className="hint quick-custom">Customized below.</p>}
    </fieldset>
  );
}

function Disclosure({ summary, children, open }: { summary: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="tab-disclosure" open={open}>
      <summary>{summary}</summary>
      {children}
    </details>
  );
}

/** Whole-number percentages that total exactly 100 (largest remainder). */
export function toPercent(values: Record<string, number>, keys: string[]): Record<string, number> {
  const total = keys.reduce((a, k) => a + Math.max(0, Number(values[k]) || 0), 0);
  if (!total) return Object.fromEntries(keys.map((k, i) => [k, i === 0 ? 100 : 0]));
  const raw = keys.map((k) => (Math.max(0, Number(values[k]) || 0) / total) * 100);
  return fill(keys, raw, 100);
}
function fill(keys: string[], raw: number[], target: number) {
  const floor = raw.map(Math.floor);
  let left = target - floor.reduce((a, b) => a + b, 0);
  const order = raw.map((v, i) => [v - Math.floor(v), i]).sort((a, b) => b[0] - a[0]);
  for (const [, i] of order) if (left-- > 0) floor[i]++;
  return Object.fromEntries(keys.map((k, i) => [k, floor[i]]));
}
/** Set one share and scale the others proportionally so the total stays 100. */
export function rebalance(pct: Record<string, number>, key: string, value: number): Record<string, number> {
  const others = Object.keys(pct).filter((k) => k !== key);
  const target = 100 - value, rest = others.reduce((a, k) => a + pct[k], 0);
  const raw = others.map((k) => (rest ? (pct[k] / rest) * target : target / others.length));
  return { ...fill(others, raw, target), [key]: value };
}

/** Sliders that always total 100%, shown with one stacked bar. */
export function ShareSliders({ label, items, values, onChange }: {
  label: string;
  items: { key: string; name: string; line: string }[];
  values: Record<string, number>;
  onChange: (v: Record<string, number>) => void;
}) {
  const keys = items.map((i) => i.key), pct = toPercent(values, keys);
  return (
    <fieldset className="share-sliders">
      <legend>{label}</legend>
      <div className="share-bar" aria-hidden="true">
        {items.map((it, i) => pct[it.key] > 0 && (
          <span key={it.key} style={{ flexGrow: pct[it.key], background: PALETTE[i % PALETTE.length] }}>
            {pct[it.key] >= 12 ? `${pct[it.key]}%` : ""}
          </span>
        ))}
      </div>
      {items.map((it, i) => (
        <RangeField key={it.key} label={it.name} hint={it.line} swatch={PALETTE[i % PALETTE.length]} min={0} max={100}
          value={pct[it.key]} onChange={(v) => onChange(rebalance(pct, it.key, v))} readout={`${pct[it.key]}%`}
          valueText={`${pct[it.key]}% of visits`} />
      ))}
      <p className="hint">Moving one slider rebalances the others; the total stays 100%.</p>
    </fieldset>
  );
}

function Chips<T extends string>({ label, options, value, onChange, multiple = false }: {
  label: string;
  options: [T, string][];
  value: T[];
  onChange: (v: T[]) => void;
  multiple?: boolean;
}) {
  const name = useId();
  return (
    <fieldset className="chip-group compact">
      <legend>{label}</legend>
      {options.map(([k, text]) => (
        <label key={k} className="chip">
          <input type={multiple ? "checkbox" : "radio"} name={name} checked={value.includes(k)}
            onChange={(e) => onChange(multiple ? (e.target.checked ? [...value, k] : value.filter((x) => x !== k)) : [k])} />
          <span>{text}</span>
        </label>
      ))}
    </fieldset>
  );
}

/* ---------- Visitors ---------- */

const NETWORKS: { key: string; name: string; line: string }[] = [
  { key: "wifi", name: "Wi-Fi", line: "Home and office connections" },
  { key: "fast-4g", name: "Fast 4G", line: "Good mobile signal" },
  { key: "slow-4g", name: "Slow 4G", line: "Weak or busy mobile signal" },
  { key: "fast-3g", name: "Fast 3G", line: "Older mobile networks" },
  { key: "slow-3g", name: "Slow 3G", line: "Very poor mobile signal" },
];
const VISITORS = [
  { id: "typical", name: "Typical mix", line: "45% returning; app restarts and interruptions at usual levels.", returning: 45, coldStarts: 35, interruptions: 12 },
  { id: "new", name: "Mostly new visitors", line: "15% returning, as after a campaign or launch.", returning: 15, coldStarts: 60, interruptions: 12 },
  { id: "loyal", name: "Mostly returning", line: "75% returning users who know the app.", returning: 75, coldStarts: 25, interruptions: 15 },
];

export function VisitorsTab({ draft, update }: { draft: Draft; update: (d: Partial<Draft>) => void }) {
  const native = draft.apps.some((a) => a.platform !== "web");
  const chosen = VISITORS.find((v) => v.returning === draft.returning && (!native || (v.coldStarts === draft.coldStarts && v.interruptions === draft.interruptions)))?.id || "custom";
  const mix = draft.populationOverrides?.network_mix;
  return (
    <>
      <TabIntro purpose="Who visits and how often they come back." effect="Changes new versus returning users, app start types and connection types." />
      <QuickChoices label="Visitors" options={VISITORS} value={chosen}
        onChange={(id) => { const v = VISITORS.find((x) => x.id === id)!; update({ returning: v.returning, coldStarts: v.coldStarts, interruptions: v.interruptions }); }} />
      <Disclosure summary="Customize">
        <div className="range-stack">
          <RangeField label="Returning visitors" hint="Visits from people who came before, with the same device and user." min={0} max={100}
            value={draft.returning} onChange={(v) => update({ returning: v })} readout={`${draft.returning}% of visits`} valueText={`${draft.returning}%`} />
          {native && (
            <>
              <RangeField label="Cold starts on return" hint="Returning app visits that launch the app from scratch." min={0} max={100}
                value={draft.coldStarts} onChange={(v) => update({ coldStarts: v })} readout={`${draft.coldStarts}% of returns`} valueText={`${draft.coldStarts}%`} />
              <RangeField label="App interruptions" hint="Visits where the app goes to the background mid-journey." min={0} max={100}
                value={draft.interruptions} onChange={(v) => update({ interruptions: v })} readout={`${draft.interruptions}% of visits`} valueText={`${draft.interruptions}%`} />
            </>
          )}
        </div>
        <Chips label="Connection types" value={[mix ? "custom" : "device"]} options={[["device", "Match each device"], ["custom", "Set the mix"]]}
          onChange={([v]) => update({ populationOverrides: { ...draft.populationOverrides, network_mix: v === "custom" ? { wifi: 45, "fast-4g": 25, "slow-4g": 20, "fast-3g": 8, "slow-3g": 2 } : undefined } })} />
        {mix && <ShareSliders label="Connection mix" items={NETWORKS} values={mix}
          onChange={(v) => update({ populationOverrides: { ...draft.populationOverrides, network_mix: v } })} />}
        <Disclosure summary="Expert">
          <div className="field-grid">
            <Field label="Visit patterns" hint="Previous distribution keeps the earlier fixed journey depth.">
              <select value={draft.population} onChange={(e) => update({ population: e.target.value as Draft["population"] })}>
                <option value="varied">Varied visits</option>
                <option value="legacy">Previous distribution</option>
              </select>
            </Field>
          </div>
        </Disclosure>
      </Disclosure>
    </>
  );
}

/* ---------- User journeys ---------- */

export const INTENTS: Record<string, Record<string, [string, string]>> = {
  retail: {
    browser: ["Browsing only", "Views a few products, doesn't buy"],
    buyer: ["Buys", "Completes checkout"],
    cart_abandoner: ["Leaves at cart", "Adds to cart, doesn't check out"],
    searcher: ["Searches", "Searches and compares results"],
    bouncer: ["Leaves after one page", "Opens one page and leaves"],
    power_user: ["Frequent shopper", "Views many products, then buys"],
  },
  banking: {
    quick_check: ["Checks balances", "Signs in and views accounts"],
    history_review: ["Reviews transactions", "Reads recent account activity"],
    transfer: ["Sends a transfer", "Completes a transfer"],
    transfer_abandoner: ["Starts a transfer, stops", "Leaves before confirming"],
    statement: ["Downloads a statement", "Opens a monthly statement"],
    power_user: ["Uses many features", "Long visit across accounts and payments"],
  },
  travel: {
    looker: ["Looks at offers", "Opens an offer, doesn't book"],
    searcher: ["Searches", "Searches flights or hotels"],
    booker: ["Books", "Completes a booking"],
    booking_abandoner: ["Leaves at booking", "Starts a booking, doesn't finish"],
    itinerary: ["Checks a trip", "Opens an existing itinerary"],
    power_user: ["Frequent traveler", "Searches, compares and books"],
  },
};
const JOURNEY_MIXES: Record<string, Record<string, Record<string, number>>> = {
  buying: {
    retail: { browser: 15, buyer: 40, cart_abandoner: 15, searcher: 15, bouncer: 5, power_user: 10 },
    banking: { quick_check: 20, history_review: 20, transfer: 35, transfer_abandoner: 10, statement: 10, power_user: 5 },
    travel: { looker: 20, searcher: 20, booker: 35, booking_abandoner: 15, itinerary: 5, power_user: 5 },
  },
  browsing: {
    retail: { browser: 45, buyer: 8, cart_abandoner: 12, searcher: 20, bouncer: 13, power_user: 2 },
    banking: { quick_check: 45, history_review: 35, transfer: 8, transfer_abandoner: 5, statement: 5, power_user: 2 },
    travel: { looker: 45, searcher: 35, booker: 5, booking_abandoner: 8, itinerary: 5, power_user: 2 },
  },
};
const JOURNEYS = [
  { id: "balanced", name: "Balanced", line: "The usual mix of browsing, searching and buying." },
  { id: "buying", name: "Mostly buying", line: "More checkouts, transfers and bookings." },
  { id: "browsing", name: "Mostly browsing", line: "More looking around, fewer completed purchases." },
];
const same = (a?: Record<string, number>, b?: Record<string, number>) => !!a && !!b && Object.keys(b).every((k) => a[k] === b[k]);

export function JourneysTab({ draft, update, caps }: { draft: Draft; update: (d: Partial<Draft>) => void; caps: Json }) {
  const lobs = Object.keys(caps.population_model || {}).filter((lob) => draft.flow === "all" || draft.flow === lob);
  const mix = draft.populationOverrides?.intent_mix;
  const chosen = !mix || !Object.keys(mix).length ? "balanced"
    : Object.keys(JOURNEY_MIXES).find((id) => lobs.every((lob) => same(mix[lob], JOURNEY_MIXES[id][lob]))) || "custom";
  return (
    <>
      <TabIntro purpose="What people come to do." effect="Changes which screens, flows and purchases sessions contain. A purchase still needs every earlier step to succeed." />
      <QuickChoices label="User journeys" options={JOURNEYS} value={chosen}
        onChange={(id) => update({ population: "varied", populationOverrides: { ...draft.populationOverrides,
          intent_mix: id === "balanced" ? undefined : structuredClone(JOURNEY_MIXES[id]) } })} />
      <Disclosure summary="Customize">
        {lobs.map((lob) => (
          <ShareSliders key={lob} label={flowNames[lob]}
            items={Object.keys(caps.population_model[lob]).map((k) => ({ key: k, name: INTENTS[lob]?.[k]?.[0] || k, line: INTENTS[lob]?.[k]?.[1] || "" }))}
            values={mix?.[lob] || caps.population_model[lob]}
            onChange={(v) => update({ population: "varied", populationOverrides: { ...draft.populationOverrides, intent_mix: { ...mix, [lob]: v } } })} />
        ))}
      </Disclosure>
    </>
  );
}

/* ---------- Speed ---------- */

export const QUANTITIES: Record<string, { name: string; noun: string; verb: string; min: number; max: number; line: string }> = {
  network_rtt_ms: { name: "Network latency", noun: "requests", verb: "take", min: 5, max: 3000, line: "Time a request and its response spend in transit." },
  backend_work_ms: { name: "Backend processing time", noun: "requests", verb: "take", min: 1, max: 10000, line: "Time the API gateway spends on each request." },
  reading_ms: { name: "Time spent reading a page", noun: "pauses", verb: "last", min: 500, max: 60000, line: "Pause between a person's actions." },
  typing_ms: { name: "Typing speed", noun: "characters", verb: "take", min: 30, max: 3000, line: "Time per typed character." },
};
export const quantityName = (q: string) => QUANTITIES[q]?.name || q;
const SPEEDS = [
  { id: "typical", name: "Typical", line: "Device, network and service defaults." },
  { id: "network", name: "Slow network", line: "Requests spend about 150 ms in transit; some take far longer." },
  { id: "backend", name: "Slow backend", line: "Every request waits longer on backend processing." },
  { id: "tail", name: "Tail-only slowdown", line: "Most requests stay fast; the slowest 20% become 4× slower." },
];
const SPEED_IDS: Record<string, string> = { network: "scenario-network", backend: "scenario-backend", tail: "scenario-tail" };

function speedBinding(caps: Json, id: string): Json {
  const q = id === "network" ? "network_rtt_ms" : "backend_work_ms", distribution = structuredClone(caps.modeling.defaults[q]);
  distribution.parameters = { network: { mu: Math.log(150), sigma: 0.65 }, backend: { mu: Math.log(120), sigma: 1 }, tail: { mu: Math.log(50), sigma: 0.8 } }[id];
  return { id: SPEED_IDS[id], quantity: q, distribution, share: id === "tail" ? 0.5 : 1,
    ...(id === "tail" ? { effect: { kind: "tail", factor: 4, knee: 0.8 } } : {}) };
}

export function SpeedTab({ draft, update, caps, index, setIndex }: {
  draft: Draft; update: (d: Partial<Draft>) => void; caps: Json; index: number; setIndex: (i: number) => void;
}) {
  const meta = caps.modeling;
  const model = draft.modeling || { version: "1.0", bindings: [], experiments: [] };
  const bindings: Json[] = model.bindings || [];
  const i = Math.min(index, bindings.length - 1), active = bindings[i];
  const setModel = (v: Json) => update({ modeling: v, modelingPreset: "custom" });
  const patch = (b: Json) => setModel({ ...model, bindings: bindings.map((v, j) => (j === i ? b : v)) });
  const chosen = !bindings.length ? "typical"
    : bindings.length === 1 ? Object.keys(SPEED_IDS).find((k) => SPEED_IDS[k] === bindings[0].id) || "custom" : "custom";
  const cohort = active?.cohort || {}, setCohort = (c: Json) => patch({ ...active, cohort: { ...cohort, ...c } });
  return (
    <>
      <TabIntro purpose="How fast pages, networks and backends respond." effect="Changes durations of network requests, traces and the pauses between actions." />
      <QuickChoices label="Speed" options={SPEEDS} value={chosen} onChange={(id) => {
        if (id === "typical") update({ modeling: { ...model, bindings: [] }, modelingPreset: model.experiments?.length || model.releases?.length ? "custom" : undefined });
        else update({ modelingPreset: id === "network" ? "network-profile" : id === "tail" ? "tail" : "custom", modeling: { ...model, bindings: [speedBinding(caps, id)] } });
        setIndex(0);
      }} />
      <Disclosure summary="Customize" open={chosen === "custom"}>
        <div className="editor-toolbar">
          {bindings.length > 1 && (
            <Field label="Speed change">
              <select value={i} onChange={(e) => setIndex(Number(e.target.value))}>
                {bindings.map((b, j) => <option key={b.id} value={j}>{quantityName(b.quantity)} · {j + 1}</option>)}
              </select>
            </Field>
          )}
          <Field label="Add a speed change for">
            <select value="" onChange={(e) => {
              const q = e.target.value;
              if (!q) return;
              setModel({ ...model, bindings: [...bindings, { id: q.replace("_ms", "") + "-" + Date.now().toString(36), quantity: q,
                priority: Math.max(0, ...bindings.map((b) => b.priority || 0)) + 1, distribution: structuredClone(meta.defaults[q]) }] });
              setIndex(bindings.length);
            }}>
              <option value="">Choose what to change</option>
              {Object.keys(QUANTITIES).map((q) => <option key={q} value={q}>{QUANTITIES[q].name}</option>)}
            </select>
          </Field>
        </div>
        {active ? (
          <div className="speed-change">
            <h3>{quantityName(active.quantity)}</h3>
            <p className="hint">{QUANTITIES[active.quantity]?.line}</p>
            <DistributionEditor binding={active} metadata={meta} change={patch} scale={QUANTITIES[active.quantity]}>
              <div className="field-grid">
                <Field label="Rule priority" hint="When rules overlap, the higher number wins.">
                  <input type="number" min="0" max="1000" value={active.priority || 0} onChange={(e) => patch({ ...active, priority: Number(e.target.value) })} />
                </Field>
                {["networks", "intents", "app_versions", "screens", ...(["backend_work_ms", "network_rtt_ms"].includes(active.quantity) ? ["endpoints"] : [])].map((key) => (
                  <Field key={key} label={"Only " + key.replaceAll("_", " ")} hint="Comma-separated internal names; empty means all.">
                    <input value={(cohort[key] || []).join(", ")} onChange={(e) => setCohort({ [key]: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} />
                  </Field>
                ))}
              </div>
            </DistributionEditor>
            <h4>Who is affected</h4>
            <Chips label="Platforms" multiple options={draft.apps.map((a) => [a.platform, a.platform === "ios" ? "iOS" : a.platform === "web" ? "Web" : "Android"] as [string, string])}
              value={cohort.platforms?.length ? cohort.platforms : draft.apps.map((a) => a.platform)}
              onChange={(v) => setCohort({ platforms: v.length === draft.apps.length ? [] : v })} />
            <Chips label="Devices" multiple options={[["flagship", "Flagship"], ["mid", "Mid-range"], ["budget", "Budget"]]}
              value={cohort.device_tiers?.length ? cohort.device_tiers : ["flagship", "mid", "budget"]}
              onChange={(v) => setCohort({ device_tiers: v.length === 3 ? [] : v })} />
            <Chips label="Visitors" options={[["all", "Everyone"], ["false", "New"], ["true", "Returning"]]}
              value={[cohort.returning === undefined ? "all" : String(cohort.returning)]}
              onChange={([v]) => { const c = { ...cohort }; if (v === "all") delete c.returning; else c.returning = v === "true"; patch({ ...active, cohort: c }); }} />
            {(model.experiments || []).map((x: Json) => (
              <Chips key={x.id} label={"A/B test " + x.id} options={[["", "Any group"], ...Object.keys(x.variants).map((v) => [v, v] as [string, string])]}
                value={[cohort.experiments?.[x.id]?.[0] || ""]}
                onChange={([v]) => { const ex = { ...(cohort.experiments || {}) }; if (v) ex[x.id] = [v]; else delete ex[x.id]; setCohort({ experiments: ex }); }} />
            ))}
            <RangeField label="Share of matching visits" min={0} max={100} value={Math.round((active.share ?? 1) * 100)}
              onChange={(v) => patch({ ...active, share: v / 100 })} readout={`${Math.round((active.share ?? 1) * 100)}% of visits`} valueText={`${Math.round((active.share ?? 1) * 100)}%`} />
            <Chips label="Make it slower" options={[["none", "No"], ["scale", "Everything"], ["tail", "Slowest only"]]} value={[active.effect?.kind || "none"]}
              onChange={([v]) => { const next = { ...active }; if (v === "none") delete next.effect; else next.effect = { kind: v, factor: active.effect?.factor || 4, knee: active.effect?.knee ?? 0.8 }; patch(next); }} />
            {active.effect && (
              <div className="range-stack">
                <RangeField label="How much slower" min={1} max={20} step={0.5} value={active.effect.factor}
                  onChange={(v) => patch({ ...active, effect: { ...active.effect, factor: v } })} readout={`${active.effect.factor}× slower`} valueText={`${active.effect.factor} times slower`} />
                {active.effect.kind === "tail" && (
                  <RangeField label="Slowest share affected" min={1} max={50} value={Math.round((1 - (active.effect.knee ?? 0.8)) * 100)}
                    onChange={(v) => patch({ ...active, effect: { ...active.effect, knee: 1 - v / 100 } })}
                    readout={`Slowest ${Math.round((1 - (active.effect.knee ?? 0.8)) * 100)}%`} valueText={`slowest ${Math.round((1 - (active.effect.knee ?? 0.8)) * 100)} percent`} />
                )}
              </div>
            )}
            <Button kind="quiet" onClick={() => { const rest = bindings.filter((_, j) => j !== i); update({ modeling: { ...model, bindings: rest } }); setIndex(0); }}>Remove this speed change</Button>
          </div>
        ) : (
          <p className="hint">No speed changes. Choose one above or add one.</p>
        )}
      </Disclosure>
    </>
  );
}

/* ---------- A/B tests ---------- */

export const ACTIONS: Record<string, Record<string, string>> = {
  retail: { home: "Opens the home page", list_products: "Browses a category", search: "Searches products", get_product: "Opens a product",
    get_recommendations: "Sees recommendations", add_to_cart: "Adds to cart", get_cart: "Opens the cart", shipping_options: "Chooses shipping",
    place_order: "Places an order", get_order: "Sees the order confirmation", login: "Signs in" },
  banking: { login: "Signs in", mfa_verify: "Enters a sign-in code", accounts: "Opens accounts", transactions: "Views transactions",
    statement: "Opens a statement", payees: "Opens payees", create_transfer: "Sends a transfer", pay_bill: "Pays a bill", lock_card: "Locks a card" },
  travel: { locations: "Searches destinations", flight_search: "Searches flights", hotel_search: "Searches hotels", offer: "Opens an offer",
    create_booking: "Books a trip", trips: "Opens trips" },
};
const actionName = (endpoint: string) => Object.values(ACTIONS).map((m) => m[endpoint]).find(Boolean) || endpoint;
/** Spec IDs allow lowercase letters, digits, hyphens and underscores, starting with a letter. */
export const slug = (name: string, final = false) => {
  // Keep a trailing hyphen while typing so a space can start the next word; trim it on blur.
  let s = name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+/, "").slice(0, 40);
  if (final) s = s.replace(/-+$/, "");
  return /^[a-z]/.test(s) ? s : ("test-" + s).slice(0, 40);
};

export function ABTestsTab({ draft, update }: { draft: Draft; update: (d: Partial<Draft>) => void }) {
  const model = draft.modeling || { version: "1.0", bindings: [], experiments: [] };
  const tests: Json[] = model.experiments || [];
  const setModel = (v: Json) => update({ modeling: v, modelingPreset: "custom" });
  const lobs = Object.keys(ACTIONS).filter((lob) => draft.flow === "all" || draft.flow === lob);
  const firstAction = Object.keys(ACTIONS[lobs[0]])[0];
  return (
    <>
      <TabIntro purpose="Split users into groups you can compare." effect="Each session gets the property experiment_<name> with its group (control or treatment)." />
      <p className="hint">To compare groups, make one slower under Speed → Customize → Who is affected.</p>
      {tests.map((x, i) => {
        const change = (v: Json) => setModel({ ...model, experiments: tests.map((y, j) => (i === j ? { ...y, ...v } : y)) });
        const keys = Object.keys(x.variants), pct = toPercent(x.variants, keys), two = keys.length === 2;
        return (
          <section className="ab-test" key={i} aria-label={"A/B test " + x.id}>
            <div className="field-grid">
              <Field label="Test name" hint={`Session property: experiment_${x.id}`}>
                <input value={x.id} onChange={(e) => change({ id: slug(e.target.value) })} onBlur={(e) => change({ id: slug(e.target.value, true) })} />
              </Field>
              <Field label="Starts when users reach">
                <select value={x.exposure_endpoints[0] || ""} onChange={(e) => change({ exposure_endpoints: [e.target.value] })}>
                  {lobs.map((lob) => (
                    <optgroup key={lob} label={flowNames[lob]}>
                      {Object.entries(ACTIONS[lob]).map(([k, v]) => <option key={lob + k} value={k}>{v}</option>)}
                    </optgroup>
                  ))}
                </select>
              </Field>
            </div>
            <div className="range-stack">
              <RangeField label="Users in the test" hint="Others are not assigned a group." min={0} max={100} value={Math.round((x.enrollment ?? 1) * 100)}
                onChange={(v) => change({ enrollment: v / 100 })} readout={`${Math.round((x.enrollment ?? 1) * 100)}% of users`} valueText={`${Math.round((x.enrollment ?? 1) * 100)}%`} />
              {two && (
                <RangeField label="Split" min={0} max={100} value={pct[keys[0]]}
                  onChange={(v) => change({ variants: { [keys[0]]: v, [keys[1]]: 100 - v } })}
                  readout={`${keys[0]} ${pct[keys[0]]}% · ${keys[1]} ${pct[keys[1]]}%`} valueText={`${keys[0]} ${pct[keys[0]]} percent, ${keys[1]} ${pct[keys[1]]} percent`} />
              )}
            </div>
            {!two && <ShareSliders label="Groups" items={keys.map((k) => ({ key: k, name: k, line: "" }))} values={x.variants} onChange={(v) => change({ variants: v })} />}
            <Disclosure summary="Expert">
              <div className="field-grid">
                <Field label="Assignment version" hint="Changing it reassigns every user.">
                  <input value={x.version || "1"} onChange={(e) => change({ version: e.target.value })} />
                </Field>
                <Field label="Starts at any of" hint="Comma-separated internal action names.">
                  <input value={x.exposure_endpoints.join(", ")} onChange={(e) => change({ exposure_endpoints: e.target.value.split(",").map((v: string) => v.trim()).filter(Boolean) })} />
                </Field>
              </div>
              <p className="hint">Starts at: {x.exposure_endpoints.map(actionName).join(", ")}</p>
            </Disclosure>
            <Button kind="quiet" onClick={() => setModel({ ...model, experiments: tests.filter((_, j) => j !== i) })}>Remove this test</Button>
          </section>
        );
      })}
      <Button onClick={() => {
        let id = "checkout-redesign", n = 2;
        while (tests.some((x) => x.id === id)) id = "checkout-redesign-" + n++;
        setModel({ ...model, experiments: [...tests, { id, version: "1", enrollment: 1, variants: { control: 50, treatment: 50 },
          exposure_endpoints: [lobs.includes("retail") ? "get_product" : firstAction] }] });
      }}>{tests.length ? "Add another test" : "Add an A/B test"}</Button>
    </>
  );
}

/* ---------- App versions ---------- */

const RELEASES = [
  { id: "steady", name: "Steady mix", line: "Versions keep their usual shares for the whole run." },
  { id: "rollout", name: "New version rolls out", line: "The newest version grows from 10% to 80%." },
  { id: "rollback", name: "Rollback", line: "The newest version drops from 60% to 5% after a bad release." },
];

/** A two-point curve: the chosen version's share moves from `from` to `to` over `minutes`; others share the rest. */
export function releaseCurve(entry: Json, version: string, from: number, to: number, minutes: number, rollback = false): Json {
  const others = entry.versions.filter((v: Json) => v.version !== version);
  const weight = others.reduce((a: number, v: Json) => a + (v.share || 1), 0) || 1;
  const shares = (share: number) => {
    const raw = others.map((v: Json) => ((v.share || 1) / weight) * (100 - share));
    const out = fill(others.map((v: Json) => v.version), raw, 100 - share);
    return { [version]: share, ...out };
  };
  return { target: entry.target, start: "run", ...(rollback ? { allow_rollback: true } : {}),
    points: [{ minute: 0, shares: shares(from) }, { minute: Math.max(1, Math.round(minutes)), shares: shares(to) }] };
}

function Adoption({ curve, version }: { curve: Json; version: string }) {
  const pts: Json[] = curve.points, last = pts[pts.length - 1].minute || 1, w = 240, h = 60;
  const path = pts.map((p, i) => `${i ? "L" : "M"}${((p.minute / last) * w).toFixed(1)},${(h - 4 - ((p.shares[version] || 0) / 100) * (h - 8)).toFixed(1)}`).join("");
  return (
    <figure className="adoption">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={`${version} share from ${pts[0].shares[version] || 0}% to ${pts[pts.length - 1].shares[version] || 0}% over ${last} minutes`}>
        <line x1="0" x2={w} y1={h - 4} y2={h - 4} />
        <path d={path} vectorEffect="non-scaling-stroke" />
      </svg>
      <figcaption><span>Start · {pts[0].shares[version] || 0}%</span><span>{last} min · {pts[pts.length - 1].shares[version] || 0}%</span></figcaption>
    </figure>
  );
}

export function AppVersionsTab({ draft, update, caps }: { draft: Draft; update: (d: Partial<Draft>) => void; caps: Json }) {
  const model = draft.modeling || { version: "1.0", bindings: [], experiments: [] };
  const curves: Json[] = model.releases || [];
  const setModel = (v: Json) => update({ modeling: v, modelingPreset: "custom" });
  const T = Math.min(10080, runMinutes(draft));
  const entries = draft.apps.map((a) => ({ app: a, entry: caps.release_catalog[a.platform] }));
  const newest = (entry: Json) => entry.versions[0].version;
  // A quick choice is recognised when every curve is the two-point shape it creates.
  const kindOf = (c: Json, entry: Json) => {
    if (c.points.length !== 2) return "custom";
    const v = newest(entry), a = c.points[0].shares[v], b = c.points[1].shares[v];
    return a === 10 && b === 80 ? "rollout" : a === 60 && b === 5 ? "rollback" : "custom";
  };
  const kinds = entries.map(({ entry }) => { const c = curves.find((x) => x.target === entry.target); return c ? kindOf(c, entry) : "none"; });
  const chosen = !curves.length ? "steady" : kinds.every((k) => k === "rollout") ? "rollout" : kinds.every((k) => k === "rollback") ? "rollback" : "custom";
  const quick = (id: string) => setModel({ ...model, releases: id === "steady" ? [] : entries.map(({ app, entry }) =>
    releaseCurve(entry, newest(entry), id === "rollout" ? 10 : 60, id === "rollout" ? 80 : 5, Math.max(1, Math.round(T * 0.6)), id === "rollback" && app.platform !== "web")) });
  return (
    <>
      <TabIntro purpose="Roll out or roll back an app version during the run." effect="Changes the version mix over time in Release Health and version filters." />
      <QuickChoices label="App versions" options={RELEASES} value={chosen} onChange={quick} />
      <Disclosure summary="Customize" open={chosen === "custom"}>
        {entries.map(({ app, entry }) => {
          const curve = curves.find((c) => c.target === entry.target);
          const versions: string[] = entry.versions.map((v: Json) => v.version);
          const set = (c: Json) => setModel({ ...model, releases: curve ? curves.map((x) => (x === curve ? c : x)) : [...curves, c] });
          const label = app.platform === "ios" ? "iOS" : app.platform === "web" ? "Web" : "Android";
          if (!curve) return (
            <section className="release-card" key={app.platform}>
              <h3>{label}</h3>
              <p className="hint">Usual version shares.</p>
              <Button onClick={() => set(releaseCurve(entry, newest(entry), 10, 80, Math.max(1, Math.round(T * 0.6))))}>Roll out a version</Button>
            </section>
          );
          const simple = curve.points.length === 2;
          const version = Object.keys(curve.points[0].shares).find((v) => versions.includes(v) && (curve.points[0].shares[v] !== curve.points[1]?.shares[v])) || Object.keys(curve.points[0].shares)[0];
          const from = curve.points[0].shares[version] || 0, to = curve.points[curve.points.length - 1].shares[version] || 0, minutes = curve.points[curve.points.length - 1].minute;
          const redo = (v: string, a: number, b: number, m: number) => set({ ...releaseCurve(entry, v, a, b, m, !!curve.allow_rollback), start: curve.start, ...(curve.salt ? { salt: curve.salt } : {}) });
          return (
            <section className="release-card" key={app.platform}>
              <h3>{label}</h3>
              {simple ? (
                <>
                  <div className="field-grid">
                    <Field label="Version to roll out">
                      <select value={version} onChange={(e) => redo(e.target.value, from, to, minutes)}>
                        {versions.map((v) => <option key={v}>{v}</option>)}
                      </select>
                    </Field>
                  </div>
                  <div className="range-stack">
                    <RangeField label="Rollout over" min={1} max={Math.max(1, T)} value={Math.min(minutes, Math.max(1, T))} onChange={(m) => redo(version, from, to, m)}
                      readout={`${Math.min(minutes, Math.max(1, T))} min of the run`} valueText={`${minutes} minutes`} />
                    <RangeField label="Share at the start" min={0} max={100} value={from} onChange={(v) => redo(version, v, to, minutes)} readout={`${from}% of visits`} valueText={`${from}%`} />
                    <RangeField label="Share at the end" min={0} max={100} value={to} onChange={(v) => redo(version, from, v, minutes)} readout={`${to}% of visits`} valueText={`${to}%`} />
                  </div>
                  <Adoption curve={curve} version={version} />
                </>
              ) : <p className="hint">This curve has {curve.points.length} points; edit it under Expert.</p>}
              <Disclosure summary="Expert">
                <ReleaseEditor apps={[app]} catalog={caps.release_catalog} curves={[curve]} change={(c) => setModel({ ...model, releases: c.length ? curves.map((x) => (x === curve ? c[0] : x)) : curves.filter((x) => x !== curve) })} />
              </Disclosure>
              <Button kind="quiet" onClick={() => setModel({ ...model, releases: curves.filter((x) => x !== curve) })}>Keep usual shares</Button>
            </section>
          );
        })}
      </Disclosure>
    </>
  );
}
