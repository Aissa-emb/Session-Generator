import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRightIcon,
  ArrowLeftIcon,
  CheckIcon,
  PlayIcon,
  PauseIcon,
  StopIcon,
  ReloadIcon,
  GlobeIcon,
  MobileIcon,
  ActivityLogIcon,
  LayersIcon,
} from "@radix-ui/react-icons";
import { api, ApiError, Json, number, time } from "./api";
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

const steps = [
  ["Destination", "Choose your apps and tenant"],
  ["Customize", "Choose the data to include"],
  ["Preview", "Inspect sessions and traces"],
  ["Send", "Run once or continuously"],
];
const descriptions: Record<Signal, string> = {
  crashes: "Fatal Android or iOS crashes with native stack traces.",
  exceptions: "Handled Android exceptions and unhandled JavaScript errors.",
  freezes: "Main-thread freezes lasting 6–10 seconds on mobile apps.",
  network: "Request timeouts and connection resets across your journeys.",
  pages: "Slower web page loads caused by a large hero image.",
  payments: "Database pool exhaustion or a payment-rail outage.",
};
const platformName = (p: string) =>
  p === "ios" ? "iOS" : p === "web" ? "Web" : "Android";
const clock = (seconds: number) => {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 3600)
    .toString()
    .padStart(2, "0")}:${Math.floor((s % 3600) / 60)
    .toString()
    .padStart(2, "0")}:${(s % 60).toString().padStart(2, "0")}`;
};
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
  const savedSpec = useQuery({
    queryKey: ["run-spec", run?.spec_id, run?.spec_version],
    queryFn: () => api(`/specs/${run?.spec_id}?version=${run?.spec_version}`),
    enabled: !!run?.spec_id,
    retry: false,
  });
  const online = status.isSuccess && !status.isError;
  const authError = [status.error, caps.error, settings.error, job.error].find(
    (e) => e instanceof ApiError && e.status === 401,
  );
  const active =
    !!runId && (!run || ["queued", "running", "paused"].includes(run.status));
  const waiting = !!read("sg_launch_attempt") && !runId;
  useEffect(() => {
    save("sg_workflow_v1", draft);
  }, [draft]);
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
  const liveBlocked =
    !dry && draft.destination === "end_to_end" && !configured && !pending;
  const runSpec = savedSpec.data?.spec;
  const duration = runSpec?.traffic?.duration_minutes || 0;
  const elapsed = run?.started
    ? Math.max(0, (run.ended || now) - run.started)
    : 0;
  const remaining = Math.max(0, duration * 60 - elapsed);
  const draining =
    run?.status === "running" && run.desired_status !== "running";
  const stale =
    !!run?.heartbeat && now - run.heartbeat > 30 && run.status === "running";
  const sending =
    online && !job.isError && !stale && run?.status === "running" && !draining;
  const runLabel =
    !online || job.isError
      ? "Connection lost"
      : stale
        ? "Worker not responding"
        : run?.status === "running"
          ? draining
            ? run.desired_status === "paused"
              ? "Pausing"
              : "Stopping"
            : duration && remaining === 0
              ? "Finishing deliveries"
              : run.dry_run
                ? "Generating locally"
                : "Sending data"
          : (
              {
                queued: "Starting",
                paused: "Paused",
                completed: "Completed",
                stopped: "Stopped",
                failed: "Run needs attention",
              } as Record<string, string>
            )[run?.status || ""] || "Loading run";
  const counters = run?.counters || {};
  return (
    <div className="app-shell">
      <aside className="workflow-rail">
        <div className="brand">
          <span className="brand-mark" />
          <div>
            CORTEX <span>XCOR</span>
            <small>SESSION GENERATOR</small>
          </div>
        </div>
        <div className="rail-intro">
          <ActivityLogIcon />
          <span>Generate telemetry</span>
        </div>
        <nav aria-label="Generation steps">
          {steps.map(([title, sub], i) => (
            <button
              key={title}
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
                <small>{sub}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="rail-bottom">
          <span className={"status-dot " + (online ? "online" : "")} />
          {online
            ? "Local server connected"
            : status.isPending
              ? "Connecting to server"
              : "Server unavailable"}
          <p>
            Configure. Preview. Send.
            <br />
            Every run starts with your choices.
          </p>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            Session Generator <span className="muted">/</span>{" "}
            <strong>{steps[step][0]}</strong>
          </span>
          <Badge tone={online ? "success" : ""}>
            {online ? "Connected" : status.isPending ? "Connecting" : "Offline"}
          </Badge>
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
          <div className="page-heading">
            <div className="eyebrow">STEP {step + 1} OF 4</div>
            <h1>
              {
                [
                  "Where should the data go?",
                  "What would you like to generate?",
                  "Preview your data",
                  runId ? "Your run" : "Ready to send",
                ][step]
              }
            </h1>
            <p>
              {
                [
                  "Connect your apps and the tenant that will receive their distributed traces.",
                  "Start with standard traffic or a regression, then choose the signals and sending schedule.",
                  "Inspect real session timelines and backend spans before starting a run.",
                  runId
                    ? "Follow your run here. You can leave this page and return to its progress."
                    : "Review the destination and schedule, then start your run.",
                ][step]
              }
            </p>
          </div>
          {error && (
            <div className="error-notice" role="alert">
              <strong>Couldn’t complete that action</strong>
              <p>{error}</p>
            </div>
          )}
          {step === 0 && (
            <>
              <Panel title="Applications" action={<Badge>Production</Badge>}>
                <p className="section-description">
                  Select the platforms you want to generate sessions for.
                </p>
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
                      hint="Five-character ID from Cortex XCOR RUM."
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
                    <Field
                      label={platformName(a.platform) + " app name"}
                      hint="Use the name shown in your RUM app."
                    >
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
                    <span>Follow each app request through the backend.</span>
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
                    <span>Generate RUM data for your apps.</span>
                  </button>
                </div>
                {draft.destination === "end_to_end" && (
                  <div className="destination-fields">
                    <Field
                      label="Chronosphere tenant"
                      hint="Enter the tenant name or its chronosphere.io URL."
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
                          ? "A token is saved on this server. Enter a value to replace it."
                          : "Optional for previews and local tests. Required to send traces."
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
                    <p className="hint full">
                      Tokens are stored securely on the server. App network-span
                      forwarding must point to this tenant to join sessions and
                      traces.
                    </p>
                  </div>
                )}
              </Panel>
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
                    <span>
                      Normal user journeys. Add individual signals below.
                    </span>
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
                    <span>Reproduce a specific failure or slowdown.</span>
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
                  Choose the signals and their share of eligible sessions.
                  Normal session activity is always included.
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
                              " Select a supported platform in step 1."}
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
                          <span>% affected</span>
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
                      hint="A single batch, spread across the last five minutes."
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
                      <Field
                        label="Sessions per minute"
                        hint="Average rate across all selected apps."
                      >
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
                        over {draft.duration} minutes. Traffic stops
                        automatically when the duration ends.
                      </span>
                    </div>
                    <p className="hint">
                      Keep the server running and your Mac awake. Pausing does
                      not extend the scheduled end time; in-flight sessions
                      finish before the run pauses.
                    </p>
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
                    <span>Deliver to the apps and tenant shown above.</span>
                  </button>
                  <button
                    disabled={waiting}
                    className={"choice " + (dry ? "selected" : "")}
                    aria-pressed={dry}
                    onClick={() => setDry(true)}
                  >
                    <strong>Local test</strong>
                    <span>
                      Generate the same payloads locally. No data leaves this
                      server.
                    </span>
                  </button>
                </div>
                {liveBlocked && (
                  <p className="warning">
                    Add a Chronosphere API token in step 1 to send traces, or
                    choose a local test.
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
                    ? "Local output checks the generator without contacting either destination."
                    : "Delivery counters show API acceptance. Visibility in Cortex also depends on app forwarding, retention, and tenant configuration."}
                </p>
                <div className="send-action">
                  <Button
                    kind="primary"
                    disabled={!online || !!busy || liveBlocked}
                    onClick={start}
                  >
                    <PlayIcon />
                    {busy
                      ? "Starting…"
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
            <>
              <Panel className="run-panel">
                <div className="run-status-header">
                  <div
                    className={
                      "sending-indicator " + (sending ? "animating" : "")
                    }
                    aria-hidden="true"
                  >
                    <span />
                    <span />
                    <span />
                    <span />
                    <span />
                  </div>
                  <div>
                    <div className="eyebrow">
                      {run?.dry_run
                        ? "LOCAL TEST · NO EXTERNAL DELIVERY"
                        : run?.mode === "live"
                          ? "CONTINUOUS SENDING"
                          : "BATCH DELIVERY"}
                    </div>
                    <h2 aria-live="polite">{runLabel}</h2>
                  </div>
                  <Badge tone={run?.dry_run ? "" : "success"}>
                    {run?.dry_run ? "Local test" : run?.status || "Connecting"}
                  </Badge>
                </div>
                <div className="run-route">
                  <div>
                    <MobileIcon />
                    <span>
                      {runSpec?.targets
                        ?.map(
                          (a: Json) =>
                            a.app_name + " · " + platformName(a.platform),
                        )
                        .join(", ") || "Your applications"}
                    </span>
                  </div>
                  <div
                    className={"sending-path " + (sending ? "animating" : "")}
                  >
                    <i />
                    <i />
                    <i />
                  </div>
                  <div>
                    <LayersIcon />
                    <span>
                      {run?.dry_run
                        ? "Local output"
                        : runSpec?.chronosphere?.tenant || "Cortex XCOR RUM"}
                    </span>
                  </div>
                </div>
                <div className="run-metrics">
                  <div>
                    <span>Sessions generated</span>
                    <strong>
                      {number(counters.sessions ?? run?.sessions ?? 0)}
                    </strong>
                  </div>
                  <div>
                    <span>
                      {run?.dry_run ? "Records generated" : "Records accepted"}
                    </span>
                    <strong>
                      {number(
                        counters.accepted ??
                          (counters.embrace_spans || 0) +
                            (counters.embrace_logs || 0) +
                            (counters.otlp_traces || 0),
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Delivery errors</span>
                    <strong className={counters.errors ? "danger" : ""}>
                      {number(counters.errors || 0)}
                    </strong>
                  </div>
                  <div>
                    <span>Elapsed</span>
                    <strong>{clock(elapsed)}</strong>
                  </div>
                </div>
                {run?.mode === "live" && (
                  <div className="run-progress">
                    <div>
                      <span>
                        {runSpec?.traffic.rate_per_minute || "—"} sessions per
                        minute · {duration || "—"} minutes
                      </span>
                      <strong>
                        {run?.ended
                          ? "Run ended"
                          : remaining
                            ? clock(remaining) + " remaining"
                            : "Finishing pending sessions"}
                      </strong>
                    </div>
                    <progress
                      aria-label="Sending duration"
                      value={Math.min(elapsed, duration * 60)}
                      max={duration * 60 || 1}
                    />
                    <p className="hint">
                      Started {time(run.started)}
                      {duration && run.started
                        ? ` · Scheduled end ${time(run.started + duration * 60)}`
                        : ""}
                      . In-flight sessions may finish after the scheduled end.
                    </p>
                  </div>
                )}
                {draining && (
                  <p className="hint">
                    {run.desired_status === "paused"
                      ? "Waiting for in-flight sessions to finish before pausing."
                      : "Finishing pending deliveries before stopping."}
                  </p>
                )}
                {stale && (
                  <p className="warning">
                    The worker hasn’t reported progress recently. Reconnect or
                    check the server log before starting another run.
                  </p>
                )}
                {job.isError && (
                  <p className="warning">
                    {job.error.message} Run status will reconnect automatically.
                  </p>
                )}
                {job.error instanceof ApiError && job.error.status === 404 && (
                  <Button onClick={again}>Return to setup</Button>
                )}
                {run?.status === "failed" && (
                  <p className="warning">
                    The run stopped unexpectedly. Review its receipts and the
                    server log before sending again; some data may already have
                    been delivered.
                  </p>
                )}
                <div className="run-controls">
                  {active && (
                    <>
                      {run?.status === "paused" ? (
                        <Button
                          onClick={() => control("resume")}
                          disabled={!!busy || !online}
                        >
                          <PlayIcon />
                          Resume
                        </Button>
                      ) : (
                        <Button
                          onClick={() => control("pause")}
                          disabled={
                            !!busy ||
                            !online ||
                            draining ||
                            run?.status !== "running"
                          }
                        >
                          <PauseIcon />
                          Pause
                        </Button>
                      )}
                      <Button
                        kind="danger-button"
                        onClick={() => control("stop")}
                        disabled={
                          !!busy ||
                          !online ||
                          run?.desired_status === "stopped" ||
                          !run
                        }
                      >
                        <StopIcon />
                        Stop
                      </Button>
                    </>
                  )}
                  {!active && (
                    <Button kind="primary" onClick={again}>
                      Create another run
                      <ArrowRightIcon />
                    </Button>
                  )}
                  <span className="hint">Run {runId.slice(0, 8)}</span>
                </div>
              </Panel>
              {!!run?.receipts?.length && (
                <details className="panel">
                  <summary>Recent delivery receipts</summary>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Destination</th>
                          <th>Type</th>
                          <th>Status</th>
                          <th>Accepted</th>
                          <th>Rejected</th>
                        </tr>
                      </thead>
                      <tbody>
                        {run.receipts.slice(0, 20).map((r: Json) => (
                          <tr key={r.id}>
                            <td>{r.target}</td>
                            <td>{r.kind}</td>
                            <td>{r.status}</td>
                            <td>{number(r.accepted)}</td>
                            <td>{number(r.rejected)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              )}
            </>
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
              <span>
                {step === 0
                  ? "You can preview before adding a token."
                  : step === 1
                    ? "Your choices are saved on this device."
                    : step === 2
                      ? "Nothing has been sent."
                      : "Ready when you are."}
              </span>
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
