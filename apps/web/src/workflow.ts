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
  story: "standard" | "regression";
  regression: string;
  flow: string;
  signals: Record<Signal, boolean>;
  rates: Record<Signal, number>;
  lcp: number;
  mode: "burst" | "live";
  count: number;
  rate: number;
  duration: number;
  symbolication: boolean;
};
export const defaults: Draft = {
  apps: [{ platform: "android", appId: "", name: "Vantra", region: "us" }],
  destination: "end_to_end",
  tenant: "",
  story: "standard",
  regression: "payments",
  flow: "all",
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
  symbolication: true,
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
export function scenario(caps: Json, d: Draft): Json {
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
    if (target.platform === "web") target.web.symbolication = d.symbolication;
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
  if (d.flow !== "all") s.population.journey_depth[d.flow] = 1;
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
