import { Json } from "./api";
export type Platform = "android" | "web" | "ios";
export type AppTarget = {
  platform: Platform;
  appId: string;
  name: string;
  region: string;
};
export type Signal =
  | "crashes"
  | "exceptions"
  | "freezes"
  | "network"
  | "pages"
  | "payments";
export type Draft = {
  apps: AppTarget[];
  destination: "end_to_end" | "sessions_only";
  tenant: string;
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
  lcp: number;
  mode: "burst" | "live";
  count: number;
  rate: number;
  duration: number;
};
export const defaults: Draft = {
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
  signals: {
    crashes: false,
    exceptions: false,
    freezes: false,
    network: false,
    pages: false,
    payments: false,
  },
  rates: {
    crashes: 10,
    exceptions: 15,
    freezes: 20,
    network: 15,
    pages: 30,
    payments: 30,
  },
  lcp: 3800,
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
  pages: "Slow page loads",
  payments: "Payment failures",
};
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
      return {
        ...structuredClone(defaults),
        ...value,
        signals: { ...defaults.signals, ...value.signals },
        rates: { ...defaults.rates, ...value.rates },
      };
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
  if (signal === "pages") return d.apps.some((a) => a.platform === "web");
  return true;
}
export function scheduleError(d: Draft) {
  if (
    d.mode === "burst" &&
    (!Number.isInteger(d.count) || d.count < 1 || d.count > 100000)
  )
    return "Choose a whole number of sessions between 1 and 100,000.";
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
  if (
    d.signals.pages &&
    available("pages", d) &&
    (!Number.isFinite(d.lcp) || d.lcp < 2500 || d.lcp > 30000)
  )
    return "Choose a slow-page LCP between 2,500 and 30,000 milliseconds.";
  return "";
}
export function scenario(caps: Json, d: Draft, symbolication = false): Json {
  const s = structuredClone(caps.default_spec);
  s.name =
    (d.story === "regression" ? "Regression · " : "Standard traffic · ") +
    flowNames[d.flow];
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
        quota: d.mode === "burst" ? "exact" : "probability",
        filters,
        details,
      });
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
    add("pages", "lcp_regression", a.platform, {
      p75_ms: d.lcp,
      cause: "hero_image",
    });
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
    }
  }
  return s;
}
