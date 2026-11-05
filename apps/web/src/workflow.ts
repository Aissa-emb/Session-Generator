import { Json } from "./api";
export type Platform = "android" | "web" | "ios";
export type AppTarget = {
  platform: Platform;
  appId: string;
  name: string;
  region: string;
};
export type Vital = "lcp" | "inp" | "cls" | "fcp" | "ttfb";
export type Signal = "crashes" | "exceptions" | "freezes" | "network" | "payments" | Vital;
export const vitalKeys: Vital[] = ["lcp", "inp", "cls", "fcp", "ttfb"];
export const isVital = (k: Signal): k is Vital => (vitalKeys as string[]).includes(k);
export type Shape = "steady" | "spike" | "rise" | "waves";
/** How one signal's share of sessions changes across the run. Positions are percentages of the run. */
export type Pattern = {
  shape: Shape;
  peakAt: number;
  width: number;
  baseline: number;
  waves: number;
};
export type Draft = {
  networkDefaultsVersion: 1;
  mixVersion: 3;
  profile: string;
  patterns: Record<Signal, Pattern>;
  modeling?: Json;
  populationOverrides?: Json;
  modelingPreset?: string;
  apps: AppTarget[];
  destination: "end_to_end" | "sessions_only";
  tenant: string;
  destinationId?: string;
  destinationName?: string;
  savedCredentials?: { chronosphere: string | null; symbol_upload: string | null };
  story: "standard" | "regression" | "causal";
  causalScenario: string;
  causalVariant: "affected" | "control" | "recovered" | "scheduled";
  causalStart: string;
  causalMinutes: number;
  causalShare: number;
  causalRegion: string;
  causalRelease: string;
  causalReference: boolean;
  causalWidget: boolean;
  causalProvider: string;
  causalMix: number[];
  causalMode: "single" | "mixed";
  causalCache: "cold" | "warm";
  causalNetwork: "wifi" | "cellular" | "constrained";
  causalFailure: "handled" | "fatal";
  causalConnectivity: "pre_send" | "post_send";
  causalSeed: number;
  regression: string;
  flow: string;
  population: "varied" | "legacy";
  returning: number;
  coldStarts: number;
  interruptions: number;
  signals: Record<Signal, boolean>;
  rates: Record<Signal, number>;
  /** p75 each web vital regression is pushed to: milliseconds, or unitless for CLS. */
  severity: Record<Vital, number>;
  mode: "burst" | "live";
  count: number;
  rate: number;
  duration: number;
};
export const steady: Pattern = { shape: "steady", peakAt: 50, width: 40, baseline: 0, waves: 3 };
const allSteady = (): Record<Signal, Pattern> =>
  Object.fromEntries(["crashes", "exceptions", "freezes", "network", "payments", ...vitalKeys].map((k) => [k, { ...steady }])) as Record<Signal, Pattern>;
export const defaults: Draft = {
  networkDefaultsVersion: 1,
  mixVersion: 3,
  profile: "baseline",
  patterns: allSteady(),
  apps: [{ platform: "android", appId: "", name: "Vantra", region: "us" }],
  destination: "end_to_end",
  tenant: "",
  story: "standard",
  causalScenario: "healthy",
  causalVariant: "affected",
  causalMode: "single",
  causalCache: "cold",
  causalNetwork: "wifi",
  causalFailure: "handled",
  causalConnectivity: "pre_send",
  causalSeed: 7331,
  causalStart: "",
  causalMinutes: 20,
  causalShare: 50,
  causalRegion: "us-east-1",
  causalRelease: "2026.10.2",
  causalReference: false,
  causalWidget: false,
  causalProvider: "none",
  causalMix: [70,15,10,5],
  regression: "payments",
  flow: "all",
  population: "varied",
  returning: 45,
  coldStarts: 35,
  interruptions: 12,
  // Default to a realistic production mix with errors on every signal.
  signals: {
    crashes: true,
    exceptions: true,
    freezes: true,
    network: true,
    payments: true,
    lcp: true,
    inp: true,
    cls: true,
    fcp: true,
    ttfb: true,
  },
  rates: {
    crashes: 8,
    exceptions: 20,
    freezes: 15,
    network: 15,
    payments: 12,
    lcp: 25,
    inp: 15,
    cls: 15,
    fcp: 10,
    ttfb: 10,
  },
  severity: { lcp: 3800, inp: 450, cls: 0.3, fcp: 3200, ttfb: 2200 },
  mode: "burst",
  count: 100,
  rate: 10,
  duration: 60,
};
export const flowNames: Record<string, string> = {
  all: "All journeys",
  retail: "Shopping & checkout",
  banking: "Banking & transfers",
  travel: "Travel & booking",
};
export const signalNames: Record<Signal, string> = {
  crashes: "Crashes",
  exceptions: "Exceptions",
  freezes: "App freezes",
  network: "Network failures",
  payments: "Payment failures",
  lcp: "LCP",
  inp: "INP",
  cls: "CLS",
  fcp: "FCP",
  ttfb: "TTFB",
};
export type SignalGroup = "errors" | "vitals";
// Colours repeat between the two groups: each group has its own chart and legend.
export const signalInfo: Record<Signal, { short: string; max: number; color: string; group: SignalGroup }> = {
  network: { short: "Timeouts and connection resets", max: 60, color: "#3987e5", group: "errors" },
  crashes: { short: "Fatal app terminations", max: 40, color: "#d95926", group: "errors" },
  exceptions: { short: "Unhandled and logged errors", max: 80, color: "#199e70", group: "errors" },
  freezes: { short: "Main thread blocked 6–10 s", max: 60, color: "#c98500", group: "errors" },
  payments: { short: "Payment backend outages", max: 60, color: "#9085e9", group: "errors" },
  lcp: { short: "Largest Contentful Paint", max: 80, color: "#3987e5", group: "vitals" },
  inp: { short: "Interaction to Next Paint · Chromium only", max: 60, color: "#d95926", group: "vitals" },
  cls: { short: "Cumulative Layout Shift · Chromium only", max: 60, color: "#199e70", group: "vitals" },
  fcp: { short: "First Contentful Paint", max: 60, color: "#c98500", group: "vitals" },
  ttfb: { short: "Time to First Byte", max: 60, color: "#d55181", group: "vitals" },
};
export const groupNames: Record<SignalGroup, string> = { errors: "Errors", vitals: "Web Vitals" };
export const signalOrder: Signal[] = ["network", "crashes", "exceptions", "freezes", "payments", ...vitalKeys];
/** Severity slider per vital: the p75 a regressed page is pushed to, from just past the good threshold. */
export const vitalScale: Record<Vital, { min: number; max: number; step: number; text: (v: number) => string; unit: string }> = {
  lcp: { min: 2500, max: 15000, step: 100, text: (v) => `${(v / 1000).toFixed(1)} s`, unit: "milliseconds" },
  inp: { min: 200, max: 2000, step: 10, text: (v) => `${Math.round(v)} ms`, unit: "milliseconds" },
  cls: { min: 0.1, max: 1, step: 0.01, text: (v) => v.toFixed(2), unit: "" },
  fcp: { min: 1800, max: 10000, step: 100, text: (v) => `${(v / 1000).toFixed(1)} s`, unit: "milliseconds" },
  ttfb: { min: 800, max: 6000, step: 50, text: (v) => `${Math.round(v)} ms`, unit: "milliseconds" },
};
type Mix = Partial<Record<Signal, [number, Partial<Pattern>?]>>;
export type Profile = { id: string; name: string; line: string; mix?: Mix; severity?: Partial<Record<Vital, number>> };
export const profiles: Profile[] = [
  { id: "baseline", name: "Production baseline", line: "Steady errors on every signal",
    mix: { network: [15], crashes: [8], exceptions: [20], freezes: [15], payments: [12],
      lcp: [25], inp: [15], cls: [15], fcp: [10], ttfb: [10] } },
  { id: "spike", name: "Incident spike", line: "Errors surge mid-run, then recover",
    mix: { network: [45, { shape: "spike", baseline: 8 }], crashes: [6], exceptions: [35, { shape: "spike", baseline: 10 }],
      freezes: [12], payments: [40, { shape: "spike" }],
      lcp: [40, { shape: "spike", baseline: 15 }], inp: [12], cls: [12], fcp: [10], ttfb: [35, { shape: "spike", baseline: 6 }] },
    severity: { lcp: 4500, ttfb: 3000 } },
  { id: "release", name: "Bad release", line: "Crashes climb after a rollout",
    mix: { network: [12], crashes: [25, { shape: "rise", peakAt: 30, width: 25, baseline: 4 }],
      exceptions: [45, { shape: "rise", peakAt: 30, width: 25, baseline: 10 }],
      freezes: [30, { shape: "rise", peakAt: 30, width: 25, baseline: 8 }], payments: [8],
      lcp: [20], inp: [40, { shape: "rise", peakAt: 30, width: 25, baseline: 8 }],
      cls: [35, { shape: "rise", peakAt: 30, width: 25, baseline: 8 }], fcp: [10], ttfb: [8] },
    severity: { inp: 650, cls: 0.35 } },
  { id: "network", name: "Flaky network", line: "Recurring timeouts and slow pages",
    mix: { network: [40, { shape: "waves", baseline: 10 }], crashes: [5], exceptions: [15], freezes: [10], payments: [10],
      lcp: [45], inp: [10], cls: [10], fcp: [35], ttfb: [30, { shape: "waves", baseline: 8 }] },
    severity: { lcp: 5200, fcp: 3800, ttfb: 2600 } },
  { id: "healthy", name: "Healthy", line: "Minimal errors for comparison",
    mix: { network: [3], crashes: [1], exceptions: [4], freezes: [3], payments: [2],
      lcp: [5], inp: [3], cls: [3], fcp: [3], ttfb: [3] },
    severity: { lcp: 2800, inp: 260, cls: 0.13, fcp: 2100, ttfb: 950 } },
  { id: "story", name: "Diagnostic story", line: "One connected client-to-backend story" },
];
export const profileName = (id: string) => profiles.find((p) => p.id === id)?.name || "Custom mix";
/** Signals, rates, patterns and vital severities for an error profile. Unlisted signals are off. */
export function profileMix(id: string): Pick<Draft, "signals" | "rates" | "patterns" | "severity"> {
  const profile = profiles.find((p) => p.id === id && p.mix) || profiles[0];
  const signals = { ...defaults.signals }, rates = { ...defaults.rates }, patterns = allSteady();
  for (const key of signalOrder) {
    const entry = profile.mix![key];
    signals[key] = !!entry;
    if (entry) {
      rates[key] = entry[0];
      patterns[key] = { ...steady, ...entry[1] };
    }
  }
  return { signals, rates, patterns, severity: { ...defaults.severity, ...profile.severity } };
}

/** Run length in minutes that patterns are positioned in: a batch is dated over the last five minutes. */
export const runMinutes = (d: Pick<Draft, "mode" | "duration">) => (d.mode === "live" ? d.duration : 5);
type Curve = { start: number; onset: number; peak: number; recovery: number; every?: number };
const round = (n: number) => Math.round(n * 1000) / 1000;
const span = (n: number) => round(Math.min(1440, Math.max(0, n)));
/**
 * The incident timing a pattern sends, in minutes from the start of the run, or null for a steady share.
 * Payment failures keep a longer peak because the pool story fails only at its peak.
 */
/** Incident windows span at most a day, so timed patterns apply to runs of up to 24 hours. */
export const MAX_PATTERN_MINUTES = 1440;
export function curveFor(key: Signal, p: Pattern, minutes: number): Curve | null {
  const T = Math.max(1, minutes), hold = key === "payments" ? 0.4 : 0.1;
  if (T > MAX_PATTERN_MINUTES) return null;
  if (p.shape === "spike") {
    const width = (Math.min(100, Math.max(10, p.width)) / 100) * T;
    const peak = Math.max(0.05, span(width * hold)), ramp = span((width - peak) / 2);
    return { start: round((p.peakAt / 100) * T - peak / 2 - ramp), onset: ramp, peak, recovery: ramp };
  }
  if (p.shape === "rise") {
    const start = (Math.min(95, Math.max(0, p.peakAt)) / 100) * T, onset = span((Math.max(5, p.width) / 100) * T);
    return { start: round(start), onset, peak: Math.max(0.05, span(T - start - onset)), recovery: 0 };
  }
  if (p.shape === "waves") {
    const every = round(Math.min(10080, Math.max(2, T / Math.max(1, p.waves))));
    const peak = Math.max(0.05, span(every * hold)), ramp = span((every - peak) / 2 * 0.9);
    return { start: 0, onset: ramp, peak, recovery: ramp, every };
  }
  return null;
}
const ease = (x: number) => (1 - Math.cos(Math.PI * Math.min(1, Math.max(0, x)))) / 2;
/** Requested share (0–100) at a minute of the run; mirrors the engine's smooth incident ramp. */
export function shareAt(key: Signal, d: Pick<Draft, "rates" | "patterns" | "mode" | "duration">, minute: number) {
  const level = d.rates[key], p = d.patterns[key], c = curveFor(key, p, runMinutes(d));
  if (!c) return level;
  const base = Math.min(level, baselineOf(key, p));
  let t = minute - c.start;
  if (c.every) t = ((t % c.every) + c.every) % c.every;
  let r = 0;
  if (t >= 0 && t < c.onset) r = c.onset ? ease(t / c.onset) : 1;
  else if (t >= c.onset && t < c.onset + c.peak) r = 1;
  else if (t >= c.onset + c.peak && t < c.onset + c.peak + c.recovery) r = ease(1 - (t - c.onset - c.peak) / c.recovery);
  return base + (level - base) * r;
}
/** The payments pool story fails only at its peak, so payment failures have no baseline. */
export const baselineOf = (key: Signal, p: Pattern) => (key === "payments" || p.shape === "steady" ? 0 : p.baseline);
export function averageShare(key: Signal, d: Pick<Draft, "rates" | "patterns" | "mode" | "duration">) {
  const T = runMinutes(d), n = 200;
  let sum = 0;
  for (let i = 0; i < n; i++) sum += shareAt(key, d, ((i + 0.5) / n) * T);
  return sum / n;
}

export function restoreDraft(raw: Partial<Draft> & Json, adoptDefaults = false): Draft {
  const value: Json = structuredClone(raw);
  // Before version 3 the only web vital was "pages" (LCP regressions) with a top-level LCP target.
  for (const field of ["signals", "rates", "patterns"]) {
    if (value[field] && "pages" in value[field]) {
      value[field].lcp = value[field].pages;
      delete value[field].pages;
    }
  }
  let profile = value.profile || "custom";
  let story = value.story === "regression" ? "standard" : value.story || "standard";
  // Signals a draft never saw come from its own preset, or stay off for a custom mix.
  const preset = profiles.some((p) => p.id === profile && p.mix) ? profileMix(profile) : null;
  const severity = { ...defaults.severity, ...preset?.severity, ...value.severity };
  if (typeof value.lcp === "number") severity.lcp = value.lcp;
  delete value.lcp;
  const fill = (field: "signals" | "rates" | "patterns", off: Json) =>
    ({ ...(preset ? preset[field] : off), ...value[field] });
  const allOff = Object.fromEntries(signalOrder.map((k) => [k, false]));
  let signals = fill("signals", allOff) as Draft["signals"];
  let rates = fill("rates", defaults.rates) as Draft["rates"];
  const patterns = fill("patterns", allSteady()) as Draft["patterns"];
  if (!value.mixVersion) {
    if (adoptDefaults) {
      // Stored drafts from before error profiles adopt the default production mix once.
      const mix = profileMix("baseline");
      ({ signals, rates } = mix);
      Object.assign(patterns, mix.patterns);
      profile = "baseline";
      story = "standard";
    } else {
      // Imported configurations keep their exact signals as a custom mix.
      if (!value.networkDefaultsVersion && !signals.network) {
        signals.network = true;
        rates.network = 15;
      }
      profile = value.story === "causal" ? "story" : "custom";
    }
  }
  if (story === "causal") profile = "story";
  return { ...structuredClone(defaults), ...value, networkDefaultsVersion: 1, mixVersion: 3, profile,
    story: story as Draft["story"], signals, rates, patterns, severity };
}
export function loadDraft(): Draft {
  try {
    const value = JSON.parse(localStorage.getItem("sg_workflow_v1") || "null");
    if (
      value &&
      Array.isArray(value.apps) &&
      value.apps.length &&
      value.apps.every(
        (a: AppTarget) =>
          ["android", "web", "ios"].includes(a.platform) &&
          typeof a.appId === "string" &&
          typeof a.name === "string",
      )
    )
      return restoreDraft(value, true);
  } catch {
    /* A stale local draft should never prevent opening the generator. */
  }
  return structuredClone(defaults);
}
export function tenantName(value: string) {
  return value
    .trim()
    .replace(/^https?:\/\//i, "")
    .replace(/\.chronosphere\.io(?:\/.*)?$/i, "")
    .replace(/\/$/, "");
}
export function connectionError(d: Draft) {
  if (d.apps.some((a) => !/^[A-Za-z0-9]{5}$/.test(a.appId.trim())))
    return "Enter the five-character app ID for each selected platform.";
  if (new Set(d.apps.map((a) => a.appId.trim())).size !== d.apps.length)
    return "Each platform needs its own app ID.";
  if (d.apps.some((a) => !a.name.trim()))
    return "Enter the app name as it appears in Cortex XCOR RUM.";
  if (
    d.destination === "end_to_end" &&
    !/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(tenantName(d.tenant))
  )
    return "Enter your Chronosphere tenant name, such as demo-sandbox.";
  return "";
}
export function available(signal: Signal, d: Draft) {
  if (signal === "crashes" || signal === "freezes")
    return d.apps.some((a) => a.platform !== "web");
  if (signal === "exceptions") return d.apps.some((a) => a.platform !== "ios");
  if (isVital(signal)) return d.apps.some((a) => a.platform === "web");
  return true;
}
export function scheduleError(d: Draft) {
  if (
    d.mode === "burst" &&
    (!Number.isInteger(d.count) || d.count < 1 || d.count > 50000)
  )
    return "Choose a whole number of sessions between 1 and 50,000.";
  if (
    d.mode === "live" &&
    (!Number.isFinite(d.rate) || d.rate <= 0 || d.rate > 600)
  )
    return "Choose a sending rate above 0 and at most 600 sessions per minute.";
  if (
    d.mode === "live" &&
    (!Number.isFinite(d.duration) || d.duration < 1 || d.duration > 10080)
  )
    return "Choose a duration between 1 minute and 7 days.";
  if (d.story === "causal") {
    if (!Number.isInteger(d.causalSeed) || d.causalSeed < 0) return "Enter a nonnegative whole-number seed.";
    if (d.causalVariant === "scheduled" && (!d.causalStart || !Number.isFinite(new Date(d.causalStart).getTime()))) return "Choose the incident start time.";
    if (d.causalMinutes < 1 || d.causalMinutes > 120) return "Choose an incident duration from 1 to 120 minutes.";
    if (d.causalShare < 0 || d.causalShare > 100) return "Choose a cohort share from 0 to 100%.";
    if (d.causalMix.some(n => !Number.isFinite(n) || n < 0 || n > 100) || !d.causalMix.some(n => n > 0)) return "Set at least one positive traffic weight.";
    return "";
  }
  for (const key of Object.keys(d.signals) as Signal[])
    if (
      d.signals[key] &&
      available(key, d) &&
      (!Number.isFinite(d.rates[key]) || d.rates[key] < 1 || d.rates[key] > 100)
    )
      return `Choose an affected percentage between 1 and 100 for ${signalNames[key].toLowerCase()}.`;
  for (const key of vitalKeys) {
    const v = d.severity[key], r = vitalScale[key];
    if (d.signals[key] && available(key, d) && (!Number.isFinite(v) || v < r.min || v > r.max))
      return `Choose a ${signalNames[key]} p75 between ${r.text(r.min)} and ${r.text(r.max)}.`;
  }
  return "";
}
export function scenario(caps: Json, d: Draft, symbolication = false, now = Date.now()): Json {
  const s = structuredClone(caps.default_spec);
  if (d.savedCredentials) s.saved_credentials = structuredClone(d.savedCredentials);
  s.name = profileName(d.profile) + " · " + flowNames[d.flow];
  s.targets = d.apps.map((a) => ({
    ...structuredClone(
      a.platform === "web"
        ? caps.web_target
        : a.platform === "ios"
          ? caps.ios_target
          : caps.default_spec.targets[0],
    ),
    embrace_app_id: a.appId.trim(),
    app_name: a.name.trim(),
    region: a.region,
  }));
  for (const target of s.targets) {
    // Real captured stacks are always emitted. Original-source resolution is
    // automatic when the server has a symbol-upload credential configured.
    if (target.platform === "web") target.web.symbolication = symbolication;
  }
  s.population.platform_mix = Object.fromEntries(
    s.targets.map((t: Json) => [t.id, 1]),
  );
  s.population.line_of_business_mix = Object.fromEntries(
    ["retail", "banking", "travel"].map((lob) => [
      lob,
      d.flow === "all" || d.flow === lob ? 1 : 0,
    ]),
  );
  Object.assign(s.population, structuredClone(d.populationOverrides || {}));
  if (d.modeling) {
    s.modeling = structuredClone(d.modeling);
    if (s.modeling.releases) {
      s.modeling.releases = s.modeling.releases.filter((c:Json)=>s.targets.some((t:Json)=>t.id===c.target));
      // "run" anchors a rollout to the run's own window: a batch is dated over the five minutes before it is sent.
      const runStart = new Date(d.mode === "live" ? now : now - 5 * 60000).toISOString();
      for (const c of s.modeling.releases) if (c.start === "run") c.start = runStart;
    }
  }
  s.population.model = d.population;
  s.population.returning_share = d.returning / 100;
  if (d.population === "varied") {
    s.population.native_cold_start_share = d.coldStarts / 100;
    s.population.native_background_share = d.interruptions / 100;
  } else if (d.flow !== "all") s.population.journey_depth[d.flow] = 1;
  s.destinations = { mode: d.destination, backend_logs: false };
  s.chronosphere =
    d.destination === "end_to_end" ? { tenant: tenantName(d.tenant) } : null;
  s.traffic =
    d.mode === "live"
      ? { mode: "live", rate_per_minute: d.rate, duration_minutes: d.duration }
      : { mode: "burst", count: d.count, window: { last_minutes: 5 } };
  s.seed = null;
  s.client_faults = [];
  s.backend_faults = [];
  s.incidents = [];
  s.rollouts = [];
  s.web_performance = [];
  if (d.story === "causal") {
    // Explicit diagnostic stories own their exact journey and timing contracts.
    delete s.modeling;
    s.population.model = "legacy";
    const domain = d.causalScenario === "travel-provider" ? "travel" : "banking";
    s.name = "Causal demo · " + d.causalScenario + " · " + d.causalVariant;
    s.population.line_of_business_mix = { retail: 0, banking: Number(domain === "banking"), travel: Number(domain === "travel") };
    s.seed = d.causalSeed;
    s.causal = { scenario: d.causalScenario, variant: d.causalVariant, mode: d.causalMode,
      cache: d.causalCache, network: d.causalNetwork, client_failure: d.causalFailure,
      connectivity_phase: d.causalConnectivity, region: d.causalRegion, service_release: d.causalRelease,
      reference: d.causalReference, provider: d.causalProvider,
      web_widget: d.causalWidget && d.causalScenario === "frontend-regression" && d.apps.every(a=>a.platform==="web"),
      mix: Object.fromEntries(["healthy","hero","slow","other"].map((k,i)=>[k,d.causalMix[i]])),
      ...(d.causalVariant === "scheduled" ? {incident_start:new Date(d.causalStart).toISOString(), incident_minutes:d.causalMinutes, cohort_share:d.causalShare/100}: {}) };
    s.destinations.backend_logs = true;
    return s;
  }
  const filters = d.flow === "all" ? {} : { lines_of_business: [d.flow] };
  // Patterns become smooth incidents placed in the run's own window: a batch is
  // dated over the five minutes before it is sent; a continuous run starts now.
  const windowStart = d.mode === "live" ? now : now - 5 * 60000;
  const schedule = (key: Signal, id: string) => {
    const c = curveFor(key, d.patterns[key], runMinutes(d));
    if (!c) return;
    const baseline = baselineOf(key, d.patterns[key]);
    s.incidents.push({
      id: id + "-curve",
      fault_ref: id,
      start: new Date(windowStart + c.start * 60000).toISOString(),
      onset_minutes: c.onset,
      peak_minutes: c.peak,
      recovery_minutes: c.recovery,
      peak_rate: d.rates[key] / 100,
      ...(baseline ? { baseline_rate: Math.min(d.rates[key], baseline) / 100 } : {}),
      curve: "smooth",
      ...(c.every ? { repeat_every_minutes: c.every } : {}),
    });
  };
  const add = (
    key: Signal,
    type: string,
    platform: Platform,
    details: Json = {},
  ) => {
    if (
      d.signals[key] &&
      caps.faults.client_faults[type]?.supported_platforms.includes(platform)
    )
      s.client_faults.push({
        id: key + "-" + platform,
        type,
        platform,
        rate: d.rates[key] / 100,
        quota: key === "network" || d.mode === "burst" ? "exact" : "probability",
        ...(key === "network" ? { steer_journey: false } : {}),
        filters,
        details,
      });
    if (d.signals[key] && caps.faults.client_faults[type]?.supported_platforms.includes(platform))
      schedule(key, key + "-" + platform);
  };
  for (const a of d.apps) {
    add(
      "crashes",
      a.platform === "ios" ? "ios_crash" : "jvm_crash",
      a.platform,
    );
    add(
      "exceptions",
      a.platform === "web" ? "js_exception" : "handled_exception",
      a.platform,
      a.platform === "web"
        ? { family: "property_undefined", handling: "unhandled_error" }
        : {},
    );
    add("freezes", "main_thread_blockage", a.platform, {
      min_ms: 6000,
      max_ms: 10000,
    });
    add("network", "network_failure", a.platform, {
      kinds: ["timeout", "reset"],
    });
    // Each vital's regression pushes affected pages' p75 to its severity; natural variation still applies.
    add("lcp", "lcp_regression", a.platform, { p75_ms: d.severity.lcp, cause: "hero_image" });
    add("inp", "interaction_latency", a.platform, { p75_ms: d.severity.inp });
    add("cls", "layout_instability", a.platform, { p75: d.severity.cls });
    add("fcp", "poor_web_vitals", a.platform, { targets: { fcp: d.severity.fcp } });
    add("ttfb", "slow_server_response", a.platform, { p75_ms: d.severity.ttfb });
  }
  if (d.signals.payments) {
    for (const variant of ["payments-db-pool", "rails-outage"]) {
      const entry = caps.faults.backend_variants[variant];
      if (d.flow === "all" || entry.lines_of_business.includes(d.flow))
        s.backend_faults.push({
          id: variant,
          variant,
          type: entry.type,
          rate: d.rates.payments / 100,
          quota: d.mode === "burst" ? "exact" : "probability",
          filters,
        });
      if (d.flow === "all" || entry.lines_of_business.includes(d.flow)) schedule("payments", variant);
    }
  }
  return s;
}
