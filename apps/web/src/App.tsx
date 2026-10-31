import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRightIcon,
  ArrowLeftIcon,
  CheckIcon,
  PlayIcon,
  ReloadIcon,
  GlobeIcon,
  MobileIcon,
  ActivityLogIcon,
  LayersIcon,
} from "@radix-ui/react-icons";
import { api, ApiError, Json, number } from "./api";
import { Badge, Button, Field, Panel } from "./ui";
import {
  AppTarget,
  Draft,
  Platform,
  Signal,
  available,
  connectionError,
  defaults,
  flowNames,
  loadDraft,
  scenario,
  scheduleError,
  signalNames,
  tenantName,
} from "./workflow";
import PreviewStep from "./PreviewStep";
import RunView from "./RunView";
import cortexLogo from "./assets/cortex-xcor-dark.svg";

const steps = [
  ["Destination", "Choose your apps and tenant"],
  ["Customize", "Choose the data to include"],
  ["Preview", "Inspect sessions and traces"],
  ["Send", "Run once or continuously"],
];
const descriptions: Record<Signal, string> = {
  crashes: "Android & iOS",
  exceptions: "Handled mobile & unhandled web errors",
  freezes: "6–10 second mobile freezes",
  network: "Timeouts & connection resets",
  pages: "Slow-loading hero image",
  payments: "Database or payment-rail outage",
};
const platformName = (p: string) =>
  p === "ios" ? "iOS" : p === "web" ? "Web" : "Android";
const read = (key: string) => {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
};
const save = (key: string, value: unknown) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Storage may be unavailable in private browsing. */
  }
};
const clear = (key: string) => {
  try {
    localStorage.removeItem(key);
  } catch {
    /* Optional persistence. */
  }
};

export default function App() {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>(loadDraft);
  const [runId, setRunId] = useState<string>(() => read("sg_run_id") || "");
  const [step, setStep] = useState(() =>
    read("sg_run_id") || read("sg_launch_attempt") ? 3 : 0,
  );
  const [preview, setPreview] = useState<Json | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [token, setToken] = useState("");
  const [symbolToken, setSymbolToken] = useState("");
  const [access, setAccess] = useState("");
  const [dry, setDry] = useState(() => !!read("sg_launch_attempt")?.dry);
  const [now, setNow] = useState(Date.now() / 1000);
  const status = useQuery({
    queryKey: ["status"],
    queryFn: () => api("/status", undefined, undefined, 5000),
    refetchInterval: 4000,
    retry: false,
  });
  const caps = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api("/capabilities"),
    retry: false,
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api("/settings"),
    retry: false,
  });
  const job = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api("/runs/" + runId, undefined, undefined, 8000),
    enabled: !!runId,
    refetchInterval: 2000,
    retry: false,
  });
  const run = job.data as Json | undefined;
  const specId = run?.spec_id || read("sg_launch_attempt")?.specId;
  const savedSpec = useQuery({
    queryKey: ["run-spec", specId, run?.spec_version],
    queryFn: () =>
      api(
        `/specs/${specId}${run?.spec_version ? `?version=${run.spec_version}` : ""}`,
      ),
    enabled: !!specId,
    retry: false,
  });
  const online = status.isSuccess && !status.isError;
  const authError = [status.error, caps.error, settings.error, job.error].find(
    (e) => e instanceof ApiError && e.status === 401,
  );
  const waiting = !!read("sg_launch_attempt") && !runId;
  useEffect(() => {
    save("sg_workflow_v1", draft);
  }, [draft]);
  useEffect(() => {
    const spec = savedSpec.data?.spec;
    if (!spec?.targets) return;
    // A recovered run may use a corrected saved destination. Carry that
    // destination into the next draft instead of restoring stale browser IDs.
    setDraft((d) => ({
      ...d,
      apps: spec.targets.map((t: Json) => ({
        platform: t.platform,
        appId: t.embrace_app_id,
        name: t.app_name,
        region: t.region,
      })),
      destination: spec.destinations.mode,
      tenant: spec.chronosphere?.tenant || "",
    }));
  }, [savedSpec.data]);
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [step]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  const update = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setPreview(null);
    setError("");
  };
  const changeApp = (platform: Platform, patch: Partial<AppTarget>) =>
    update({
      apps: draft.apps.map((a) =>
        a.platform === platform ? { ...a, ...patch } : a,
      ),
    });
  const togglePlatform = (platform: Platform) => {
    if (draft.apps.some((a) => a.platform === platform)) {
      if (draft.apps.length > 1)
        update({ apps: draft.apps.filter((a) => a.platform !== platform) });
    } else
      update({
        apps: [
          ...draft.apps,
          {
            platform,
            appId: "",
            name: platform === "web" ? "Vantra Web" : "Vantra",
            region: "us",
          },
        ],
      });
  };
  const reconnect = () => {
    setError("");
    client.invalidateQueries();
  };
  const fail = (e: unknown) =>
    setError(
      e instanceof Error ? e.message : "The request could not be completed.",
    );
  const next = async () => {
    setError("");
    if (step === 0) {
      const issue = connectionError(draft);
      if (issue) {
        setError(issue);
        return;
      }
      if (token.trim()) {
        setBusy("Saving token");
        try {
          await api(
            "/settings/secrets/chronosphere",
            { value: token.trim() },
            "PUT",
          );
          setToken("");
          await settings.refetch();
        } catch (e) {
          fail(e);
          return;
        } finally {
          setBusy("");
        }
      }
      if (symbolToken.trim() && draft.apps.some((a) => a.platform === "web")) {
        setBusy("Saving source-map token");
        try {
          await api(
            "/settings/secrets/symbol_upload",
            { value: symbolToken.trim() },
            "PUT",
          );
          setSymbolToken("");
          await settings.refetch();
        } catch (e) {
          fail(e);
          return;
        } finally {
          setBusy("");
        }
      }
      setStep(1);
    } else if (step === 1) {
      await buildPreview();
    } else if (step === 2) {
      setStep(3);
    }
  };
  const buildPreview = async () => {
    if (!caps.data) return;
    const issue = connectionError(draft) || scheduleError(draft);
    if (issue) {
      setError(issue);
      return;
    }
    setBusy("Generating preview");
    setError("");
    setStep(2);
    setPreview(null);
    try {
      setPreview(await api("/preview", scenario(caps.data, draft)));
    } catch (e) {
      fail(e);
    } finally {
      setBusy("");
    }
  };
  const start = async () => {
    if (!caps.data || busy || runId) return;
    setError("");
    setBusy("Starting");
    try {
      // Retain the same spec and key after an ambiguous response, including across reloads.
      let attempt = read("sg_launch_attempt");
      if (!attempt) {
        attempt = {
          key: crypto.randomUUID(),
          spec: scenario(caps.data, draft),
          dry,
        };
        save("sg_launch_attempt", attempt);
      }
      if (!attempt.specId) {
        const saved = await api("/specs", attempt.spec);
        attempt.specId = saved.id;
        save("sg_launch_attempt", attempt);
      }
      if (
        !attempt.dry &&
        attempt.spec.targets.some(
          (t: Json) => t.platform === "web" && t.web.symbolication,
        )
      ) {
        // No run is requested until every map is acknowledged. A failed or timed-out
        // upload is safely retried using the server's per-bundle receipt ledger.
        setBusy("Uploading source maps");
        try {
          await api(`/specs/${attempt.specId}/symbols/upload`, {
            confirm: true,
          });
        } catch (e) {
          clear("sg_launch_attempt");
          throw e;
        }
        setBusy("Starting");
      }
      const value = await api("/runs", {
        spec_id: attempt.specId,
        confirm: true,
        dry_run: attempt.dry,
        idempotency_key: attempt.key,
      });
      save("sg_run_id", value.id);
      setRunId(value.id);
      client.setQueryData(["run", value.id], value);
      clear("sg_launch_attempt");
    } catch (e) {
      fail(e);
      if (e instanceof ApiError && e.status >= 400 && e.status < 500)
        clear("sg_launch_attempt");
    } finally {
      setBusy("");
    }
  };
  const control = async (action: string) => {
    setBusy(action);
    setError("");
    try {
      const value = await api(`/runs/${runId}/${action}`, {});
      client.setQueryData(["run", runId], value);
      await job.refetch();
    } catch (e) {
      fail(e);
    } finally {
      setBusy("");
    }
  };
  const again = () => {
    clear("sg_run_id");
    setRunId("");
    setPreview(null);
    setStep(0);
    setError("");
    setDry(false);
  };
  const signalKeys = Object.keys(signalNames) as Signal[];
  const selected = signalKeys.filter(
    (k) => draft.signals[k] && available(k, draft),
  );
  const configured = settings.data?.secrets.chronosphere;
  const pending = read("sg_launch_attempt");
  const symbolsBlocked =
    !dry &&
    draft.symbolication &&
    draft.apps.some((a) => a.platform === "web") &&
    !settings.data?.secrets.symbol_upload &&
    !pending;
  const liveBlocked =
    !dry && draft.destination === "end_to_end" && !configured && !pending;
  const runSpec = savedSpec.data?.spec;
  return (
    <div className="app-shell">
      <aside className="workflow-rail">
        <div className="brand">
          <img src={cortexLogo} alt="Cortex XCOR" width="184" height="27" />
        </div>
        <div className="rail-intro">
          <ActivityLogIcon />
          <span>Generate telemetry</span>
        </div>
        <nav aria-label="Generation steps">
          {steps.map(([title, sub], i) => (
            <button
              key={title}
              title={sub}
              disabled={!!busy || !!runId || waiting || i > step}
              onClick={() => {
                setStep(i);
                setError("");
              }}
              className={
                "step-link " +
                (i === step ? "current" : i < step ? "complete" : "")
              }
              aria-current={i === step ? "step" : undefined}
            >
              <span className="step-number">
                {i < step ? <CheckIcon /> : i + 1}
              </span>
              <span>
                <strong>{title}</strong>
              </span>
            </button>
          ))}
        </nav>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            Session Generator <span className="muted">/</span>{" "}
            <strong>{steps[step][0]}</strong>
          </span>
          <span
            className="server-connection"
            title="Connection to the local generator server"
          >
            <i
              className={"status-dot " + (online ? "online" : "")}
              aria-hidden="true"
            />
            {online ? "Connected" : status.isPending ? "Connecting" : "Offline"}
          </span>
        </header>
        <main>
          {((!online && !status.isPending) || caps.isError) && (
            <div className="connection-notice" role="alert">
              <div>
                <strong>
                  {authError
                    ? "Connect to this server"
                    : "The local server is unavailable"}
                </strong>
                <p>
                  {authError
                    ? "Enter your server access token to continue."
                    : status.error?.message || caps.error?.message}
                </p>
                {authError && (
                  <div className="inline-fields">
                    <Field label="Server access token">
                      <input
                        type="password"
                        autoComplete="off"
                        value={access}
                        onChange={(e) => setAccess(e.target.value)}
                      />
                    </Field>
                    <Button
                      onClick={() => {
                        sessionStorage.setItem(
                          "sg_access_token",
                          access.trim(),
                        );
                        setAccess("");
                        reconnect();
                      }}
                    >
                      Connect
                    </Button>
                  </div>
                )}
              </div>
              <Button onClick={reconnect}>
                <ReloadIcon />
                Reconnect
              </Button>
            </div>
          )}
          {!(step === 3 && runId) && (
            <div className="page-heading">
              <h1>
                {
                  [
                    "Choose a destination",
                    "Customize traffic",
                    "Preview data",
                    runId ? "Your run" : "Ready to send",
                  ][step]
                }
              </h1>
              <p>
                {
                  [
                    "Choose your apps and trace destination.",
                    "Pick a flow, signals, and schedule.",
                    "Review a local sample before sending.",
                    runId
                      ? "Follow your run here. You can leave this page and return to its progress."
                      : "Check your choices, then start.",
                  ][step]
                }
              </p>
            </div>
          )}
          {error && (
            <div className="error-notice" role="alert">
              <strong>Couldn’t complete that action</strong>
              <p>{error}</p>
            </div>
          )}
          {step === 0 && (
            <>
              <Panel title="Applications" action={<Badge>Production</Badge>}>
                <div className="platform-picker">
                  {(["android", "ios", "web"] as Platform[]).map((p) => (
                    <button
                      key={p}
                      aria-pressed={draft.apps.some((a) => a.platform === p)}
                      onClick={() => togglePlatform(p)}
                    >
                      {p === "web" ? <GlobeIcon /> : <MobileIcon />}
                      {platformName(p)}
                      {draft.apps.some((a) => a.platform === p) && (
                        <CheckIcon />
                      )}
                    </button>
                  ))}
                </div>
                {draft.apps.map((a) => (
                  <div className="app-fields" key={a.platform}>
                    <span className="app-platform">
                      {platformName(a.platform)}
                    </span>
                    <Field
                      label={platformName(a.platform) + " app ID"}
                      hint="From your RUM app."
                    >
                      <input
                        value={a.appId}
                        maxLength={5}
                        placeholder={
                          a.platform === "web" ? "e.g. kfs9f" : "e.g. 5nrc2"
                        }
                        autoComplete="off"
                        onChange={(e) =>
                          changeApp(a.platform, { appId: e.target.value })
                        }
                      />
                    </Field>
                    <Field label={platformName(a.platform) + " app name"}>
                      <input
                        value={a.name}
                        onChange={(e) =>
                          changeApp(a.platform, { name: e.target.value })
                        }
                      />
                    </Field>
                    <Field label={platformName(a.platform) + " ingest region"}>
                      <select
                        value={a.region}
                        onChange={(e) =>
                          changeApp(a.platform, { region: e.target.value })
                        }
                      >
                        <option value="us">US</option>
                        <option value="eu">EU</option>
                      </select>
                    </Field>
                  </div>
                ))}
              </Panel>
              <Panel title="Destination">
                <div className="choice-grid two">
                  <button
                    className={
                      "choice " +
                      (draft.destination === "end_to_end" ? "selected" : "")
                    }
                    aria-pressed={draft.destination === "end_to_end"}
                    onClick={() => update({ destination: "end_to_end" })}
                  >
                    <LayersIcon />
                    <strong>Sessions + distributed traces</strong>
                    <span>RUM with linked backend traces</span>
                  </button>
                  <button
                    className={
                      "choice " +
                      (draft.destination === "sessions_only" ? "selected" : "")
                    }
                    aria-pressed={draft.destination === "sessions_only"}
                    onClick={() => update({ destination: "sessions_only" })}
                  >
                    <MobileIcon />
                    <strong>Sessions only</strong>
                    <span>RUM only</span>
                  </button>
                </div>
                {draft.destination === "end_to_end" && (
                  <div className="destination-fields">
                    <Field
                      label="Chronosphere tenant"
                      hint="Tenant name or URL"
                    >
                      <input
                        value={draft.tenant}
                        placeholder="your-tenant.chronosphere.io"
                        onChange={(e) => update({ tenant: e.target.value })}
                        onBlur={() =>
                          update({ tenant: tenantName(draft.tenant) })
                        }
                      />
                    </Field>
                    <Field
                      label="Chronosphere API token"
                      hint={
                        configured
                          ? "Saved. Enter a token to replace it."
                          : "Required for live traces"
                      }
                    >
                      <input
                        type="password"
                        autoComplete="new-password"
                        placeholder={
                          configured ? "Saved on server" : "Enter API token"
                        }
                        value={token}
                        onChange={(e) => setToken(e.target.value)}
                      />
                    </Field>
                    <details className="connection-details full">
                      <summary>Connection requirements</summary>
                      <p className="hint">
                        Tokens stay on the server. App network-span forwarding
                        must point to this tenant.
                      </p>
                    </details>
                  </div>
                )}
              </Panel>
              {draft.apps.some((a) => a.platform === "web") && (
                <Panel title="JavaScript stack traces">
                  <label className="symbol-option">
                    <input
                      type="checkbox"
                      checked={draft.symbolication}
                      onChange={(e) =>
                        update({ symbolication: e.target.checked })
                      }
                    />
                    Resolve original files and lines
                  </label>
                  {draft.symbolication && (
                    <Field
                      label="Embrace Symbol Upload token"
                      hint={
                        settings.data?.secrets.symbol_upload
                          ? "Saved on server. Maps upload before live delivery."
                          : "Embrace → Settings → API → Symbol Upload. Required for live web runs."
                      }
                    >
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={symbolToken}
                        placeholder={
                          settings.data?.secrets.symbol_upload
                            ? "Saved on server"
                            : "Enter Symbol Upload token"
                        }
                        onChange={(e) => setSymbolToken(e.target.value)}
                      />
                    </Field>
                  )}
                </Panel>
              )}
            </>
          )}
          {step === 1 && (
            <div className="customize-layout">
              <Panel title="Traffic scenario" className="traffic-options">
                <div className="choice-grid two">
                  <button
                    className={
                      "choice " + (draft.story === "standard" ? "selected" : "")
                    }
                    aria-pressed={draft.story === "standard"}
                    onClick={() =>
                      update({
                        story: "standard",
                        signals: { ...defaults.signals },
                      })
                    }
                  >
                    <strong>Standard traffic</strong>
                    <span>Everyday user journeys</span>
                  </button>
                  <button
                    className={
                      "choice " +
                      (draft.story === "regression" ? "selected" : "")
                    }
                    aria-pressed={draft.story === "regression"}
                    onClick={() =>
                      update({
                        story: "regression",
                        regression: "payments",
                        signals: { ...defaults.signals, payments: true },
                      })
                    }
                  >
                    <strong>Regression</strong>
                    <span>A specific failure or slowdown</span>
                  </button>
                </div>
                <div className="form-grid">
                  {draft.story === "regression" && (
                    <Field label="Regression to reproduce">
                      <select
                        value={draft.regression}
                        onChange={(e) => {
                          const key = e.target.value as Signal;
                          update({
                            regression: key,
                            signals: { ...defaults.signals, [key]: true },
                          });
                        }}
                      >
                        {signalKeys
                          .filter((k) => available(k, draft))
                          .map((k) => (
                            <option key={k} value={k}>
                              {signalNames[k]}
                            </option>
                          ))}
                      </select>
                    </Field>
                  )}
                  <Field label="User flow">
                    <select
                      value={draft.flow}
                      onChange={(e) => update({ flow: e.target.value })}
                    >
                      {Object.entries(flowNames).map(([k, v]) => (
                        <option key={k} value={k}>
                          {v}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
              </Panel>
              <Panel
                title="Include in the data"
                className="signal-options"
                action={<Badge>{selected.length} selected</Badge>}
              >
                <p className="section-description">
                  Share of eligible sessions.
                </p>
                <div className="signal-list">
                  {signalKeys.map((key) => (
                    <div
                      key={key}
                      className={
                        "signal-row " +
                        (!available(key, draft) ? "unavailable" : "")
                      }
                    >
                      <label>
                        <input
                          type="checkbox"
                          checked={draft.signals[key] && available(key, draft)}
                          disabled={!available(key, draft)}
                          onChange={(e) =>
                            update({
                              signals: {
                                ...draft.signals,
                                [key]: e.target.checked,
                              },
                            })
                          }
                        />
                        <span>
                          <strong>{signalNames[key]}</strong>
                          <small>
                            {descriptions[key]}
                            {!available(key, draft) &&
                              " · Requires another platform"}
                          </small>
                        </span>
                      </label>
                      {draft.signals[key] && available(key, draft) && (
                        <div className="signal-rate">
                          <input
                            aria-label={
                              signalNames[key] + " affected percentage"
                            }
                            type="number"
                            min="1"
                            max="100"
                            value={draft.rates[key]}
                            onChange={(e) =>
                              update({
                                rates: {
                                  ...draft.rates,
                                  [key]: Number(e.target.value),
                                },
                              })
                            }
                          />
                          <span>%</span>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                {draft.signals.pages && available("pages", draft) && (
                  <div className="compact-field">
                    <Field label="Slow page LCP p75 (ms)">
                      <input
                        type="number"
                        min="2500"
                        max="30000"
                        value={draft.lcp}
                        onChange={(e) =>
                          update({ lcp: Number(e.target.value) })
                        }
                      />
                    </Field>
                  </div>
                )}
              </Panel>
              <Panel title="Sending schedule" className="schedule-options">
                <div
                  className="segmented"
                  role="group"
                  aria-label="Sending schedule"
                >
                  <button
                    aria-pressed={draft.mode === "burst"}
                    onClick={() => update({ mode: "burst" })}
                  >
                    One batch
                  </button>
                  <button
                    aria-pressed={draft.mode === "live"}
                    onClick={() => update({ mode: "live" })}
                  >
                    <ActivityLogIcon />
                    Continuous sending
                  </button>
                </div>
                {draft.mode === "burst" ? (
                  <div className="compact-field">
                    <Field
                      label="Number of sessions"
                      hint="Spread over the last 5 minutes"
                    >
                      <input
                        type="number"
                        min="1"
                        max="100000"
                        value={draft.count}
                        onChange={(e) =>
                          update({ count: Number(e.target.value) })
                        }
                      />
                    </Field>
                  </div>
                ) : (
                  <>
                    <div className="form-grid">
                      <Field label="Sessions per minute" hint="Across all apps">
                        <input
                          type="number"
                          min="0.1"
                          max="600"
                          step="0.1"
                          value={draft.rate}
                          onChange={(e) =>
                            update({ rate: Number(e.target.value) })
                          }
                        />
                      </Field>
                      <Field label="Run for">
                        <select
                          value={
                            [30, 60, 120].includes(draft.duration)
                              ? draft.duration
                              : "custom"
                          }
                          onChange={(e) =>
                            update({
                              duration:
                                e.target.value === "custom"
                                  ? 90
                                  : Number(e.target.value),
                            })
                          }
                        >
                          <option value="30">30 minutes</option>
                          <option value="60">1 hour</option>
                          <option value="120">2 hours</option>
                          <option value="custom">Custom duration</option>
                        </select>
                      </Field>
                      {![30, 60, 120].includes(draft.duration) && (
                        <Field label="Duration (minutes)">
                          <input
                            type="number"
                            min="1"
                            max="10080"
                            value={draft.duration}
                            onChange={(e) =>
                              update({ duration: Number(e.target.value) })
                            }
                          />
                        </Field>
                      )}
                    </div>
                    <div className="schedule-summary">
                      <ActivityLogIcon />
                      <span>
                        About{" "}
                        <strong>
                          {number(Math.round(draft.rate * draft.duration))}{" "}
                          sessions
                        </strong>{" "}
                        · automatic stop after {draft.duration} minutes.
                      </span>
                    </div>
                    <details className="connection-details">
                      <summary>While running</summary>
                      <p className="hint">
                        Keep your Mac awake. Pausing finishes in-flight sessions
                        and does not extend the end time.
                      </p>
                    </details>
                  </>
                )}
              </Panel>
            </div>
          )}
          {step === 2 &&
            (preview || busy ? (
              <PreviewStep
                data={preview}
                busy={!!busy}
                refresh={buildPreview}
              />
            ) : (
              <Panel>
                <h2>Preview not available yet</h2>
                <p>
                  Check the message above, adjust your choices if needed, and
                  try again.
                </p>
                <Button onClick={buildPreview} disabled={!online}>
                  Generate preview
                </Button>
              </Panel>
            ))}
          {step === 3 && !runId && (
            <>
              <Panel
                title="Run summary"
                action={
                  <Badge>
                    {draft.mode === "live" ? "Continuous sending" : "One batch"}
                  </Badge>
                }
              >
                <div className="review-grid">
                  <div>
                    <span>Applications</span>
                    <strong>
                      {draft.apps
                        .map(
                          (a) =>
                            `${a.name} · ${platformName(a.platform)} (${a.appId})`,
                        )
                        .join(", ")}
                    </strong>
                  </div>
                  <div>
                    <span>Destination</span>
                    <strong>
                      {draft.destination === "end_to_end"
                        ? tenantName(draft.tenant) + ".chronosphere.io"
                        : "Cortex XCOR RUM only"}
                    </strong>
                  </div>
                  <div>
                    <span>Schedule</span>
                    <strong>
                      {draft.mode === "live"
                        ? `${draft.rate} sessions/min for ${draft.duration} minutes`
                        : `${number(draft.count)} sessions`}
                    </strong>
                  </div>
                  <div>
                    <span>User flow</span>
                    <strong>{flowNames[draft.flow]}</strong>
                  </div>
                  <div className="full">
                    <span>Included signals</span>
                    <strong>
                      {selected.length
                        ? selected
                            .map(
                              (k) => `${signalNames[k]} (${draft.rates[k]}%)`,
                            )
                            .join(" · ")
                        : "Standard session activity"}
                    </strong>
                  </div>
                </div>
              </Panel>
              <Panel title="Delivery">
                <div className="choice-grid two">
                  <button
                    disabled={waiting}
                    className={"choice " + (!dry ? "selected" : "")}
                    aria-pressed={!dry}
                    onClick={() => setDry(false)}
                  >
                    <strong>Send to destination</strong>
                    <span>Live delivery</span>
                  </button>
                  <button
                    disabled={waiting}
                    className={"choice " + (dry ? "selected" : "")}
                    aria-pressed={dry}
                    onClick={() => setDry(true)}
                  >
                    <strong>Local test</strong>
                    <span>No external delivery</span>
                  </button>
                </div>
                {liveBlocked && (
                  <p className="warning">
                    Add a Chronosphere API token in step 1 to send traces, or
                    choose a local test.
                  </p>
                )}
                {symbolsBlocked && (
                  <p className="warning">
                    Add an Embrace Symbol Upload token in Destination to resolve
                    JavaScript stacks, or choose a local test.
                  </p>
                )}
                {!dry &&
                  draft.symbolication &&
                  draft.apps.some((a) => a.platform === "web") && (
                    <p className="hint">
                      Matching source maps upload to Embrace before this run
                      starts.
                    </p>
                  )}
                {waiting && !busy && (
                  <p className="warning">
                    A start request has an unconfirmed response. Retry it to
                    recover the same run without starting a duplicate.
                  </p>
                )}
                <p className="hint">
                  {dry
                    ? "No data leaves this server."
                    : "API acceptance is shown here; confirm visibility in your destination."}
                </p>
                <div className="send-action">
                  <Button
                    kind="primary"
                    disabled={
                      !online || !!busy || liveBlocked || symbolsBlocked
                    }
                    onClick={start}
                  >
                    <PlayIcon />
                    {busy
                      ? busy === "Uploading source maps"
                        ? "Uploading source maps…"
                        : "Starting…"
                      : waiting
                        ? "Retry start request"
                        : dry
                          ? "Start local test"
                          : draft.mode === "live"
                            ? "Start continuous sending"
                            : "Send batch"}
                  </Button>
                  <span>
                    {draft.mode === "live"
                      ? "You can pause or stop at any time."
                      : "The run ends after this batch."}
                  </span>
                </div>
              </Panel>
            </>
          )}
          {step === 3 && runId && (
            <RunView
              run={run}
              spec={runSpec}
              runId={runId}
              online={online}
              fetchError={job.error}
              updatedAt={job.dataUpdatedAt}
              busy={busy}
              now={now}
              control={control}
              again={again}
            />
          )}
          {!(step === 3 && runId) && (
            <footer className="workflow-footer">
              <Button
                kind="quiet"
                disabled={step === 0 || !!busy || waiting}
                onClick={() => {
                  setStep((s) => s - 1);
                  setError("");
                }}
              >
                <ArrowLeftIcon />
                Back
              </Button>

              {step < 3 && (
                <Button
                  kind="primary"
                  onClick={next}
                  disabled={
                    !!busy || !online || !caps.data || (step === 2 && !preview)
                  }
                >
                  {busy
                    ? busy + "…"
                    : step === 1
                      ? "Generate preview"
                      : step === 2
                        ? "Continue to send"
                        : "Continue"}
                  <ArrowRightIcon />
                </Button>
              )}
            </footer>
          )}
        </main>
      </div>
    </div>
  );
}
