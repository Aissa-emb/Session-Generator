import { useState } from "react";
import {
  ArrowRightIcon,
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  GlobeIcon,
  MobileIcon,
  PauseIcon,
  PlayIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { ApiError, Json, number, pretty, time } from "./api";
import { Button, Modal } from "./ui";

const clock = (seconds: number) => {
  const s = Math.max(0, Math.floor(seconds));
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60]
    .map((v) => v.toString().padStart(2, "0"))
    .join(":");
};
const platformName = (p: string) =>
  p === "ios" ? "iOS" : p === "web" ? "Web" : "Android";
const receiptTime = (ms?: number) =>
  ms
    ? new Date(ms).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";

export default function RunView({
  run,
  spec,
  runId,
  online,
  fetchError,
  updatedAt,
  busy,
  now,
  control,
  again,
}: {
  run?: Json;
  spec?: Json;
  runId: string;
  online: boolean;
  fetchError: Error | null;
  updatedAt: number;
  busy: string;
  now: number;
  control: (action: string) => void;
  again: () => void;
}) {
  const [receipt, setReceipt] = useState<Json | null>(null);
  const [copyState, setCopyState] = useState("");
  const counters = run?.counters;
  const duration = spec?.traffic?.duration_minutes || 0;
  const elapsed = run?.started
    ? Math.max(0, (run.ended || now) - run.started)
    : null;
  const remaining =
    elapsed === null ? null : Math.max(0, duration * 60 - elapsed);
  const active = !!run && ["queued", "running", "paused"].includes(run.status);
  const draining =
    run?.status === "running" && run.desired_status !== "running";
  const stale =
    !!run?.heartbeat && now - run.heartbeat > 30 && run.status === "running";
  const uncertain = !!run && (!online || !!fetchError || stale);
  const status = !run
    ? fetchError
      ? "Run unavailable"
      : "Loading run"
    : run.status === "running"
      ? draining
        ? run.desired_status === "paused"
          ? "Pausing"
          : "Stopping"
        : duration && remaining === 0
          ? "Finishing deliveries"
          : "Running"
      : (
          {
            queued: "Starting",
            paused: "Paused",
            completed: "Completed",
            stopped: "Stopped",
            failed: "Needs attention",
          } as Record<string, string>
        )[run.status] || run.status;
  const tone =
    uncertain || !!fetchError || run?.status === "failed"
      ? "warning"
      : run?.status === "running" && !draining
        ? "positive"
        : "neutral";
  const trafficActive =
    !!run &&
    run.status === "running" &&
    !draining &&
    !uncertain &&
    !(duration && remaining === 0);
  const targets = spec?.targets || [];
  const title =
    spec?.name ||
    (targets.length === 1
      ? targets[0].app_name
      : targets.length > 1
        ? "Multi-platform run"
        : "Your run");
  // Bounded jobs expose successful payload counts; live jobs expose record counts.
  // These units must never be silently added together or presented as sessions.
  const recordCount = counters?.accepted;
  const payloadCount =
    counters &&
    [
      "embrace_spans",
      "embrace_logs",
      "otlp_traces",
      "otlp_logs",
      "otlp_metrics",
    ].some((k) => counters[k] !== undefined)
      ? [
          "embrace_spans",
          "embrace_logs",
          "otlp_traces",
          "otlp_logs",
          "otlp_metrics",
        ].reduce((n, k) => n + (counters[k] || 0), 0)
      : undefined;
  const countUnit =
    recordCount !== undefined || run?.mode === "live" ? "Records" : "Payloads";
  const receipts: Json[] = run?.receipts || [];
  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(runId);
      setCopyState("Run ID copied");
    } catch {
      setCopyState("Could not copy. Select the run ID to copy it manually.");
    }
  };

  return (
    <div className="run-view">
      <div className="run-heading">
        <div className="run-title-group">
          <div className="run-title-line">
            <h1>{title}</h1>
            <span
              className={`run-status ${tone}`}
              role="status"
              aria-live="polite"
            >
              <i aria-hidden="true" />
              {uncertain ? `Last known: ${status}` : status}
            </span>
          </div>
          <div className="run-identity">
            <span>
              {run
                ? run.dry_run
                  ? "Local test"
                  : run.mode === "live"
                    ? "Continuous sending"
                    : "Batch delivery"
                : "Loading details"}
            </span>
            <span className="metadata-divider" aria-hidden="true">
              ·
            </span>
            <span>
              Run <code title={runId}>{runId.slice(0, 8)}</code>
            </span>
            <button
              className="icon-button copy-id"
              aria-label="Copy full run ID"
              title={runId}
              onClick={copyId}
            >
              {copyState === "Run ID copied" ? <CheckIcon /> : <CopyIcon />}
            </button>
            <span className="sr-only" role="status">
              {copyState}
            </span>
            {copyState.startsWith("Could not") && (
              <span className="hint">
                {copyState} <code>{runId}</code>
              </span>
            )}
          </div>
        </div>
        <div className={"run-actions " + (active ? "has-controls" : "")}>
          {active && (
            <>
              {run?.status === "paused" ? (
                <Button
                  onClick={() => control("resume")}
                  disabled={!!busy || !online || !!fetchError}
                >
                  <PlayIcon />
                  {busy === "resume" ? "Resuming…" : "Resume"}
                </Button>
              ) : (
                <Button
                  onClick={() => control("pause")}
                  disabled={
                    !!busy ||
                    !online ||
                    !!fetchError ||
                    draining ||
                    run?.status !== "running"
                  }
                >
                  <PauseIcon />
                  {busy === "pause" ? "Pausing…" : "Pause"}
                </Button>
              )}
              <Button
                kind="danger-button"
                onClick={() => control("stop")}
                disabled={
                  !!busy ||
                  !online ||
                  !!fetchError ||
                  run?.desired_status === "stopped"
                }
              >
                <StopIcon />
                {busy === "stop" ? "Stopping…" : "Stop"}
              </Button>
            </>
          )}
          {run && !active && (
            <Button onClick={again}>
              Create another run
              <ArrowRightIcon />
            </Button>
          )}
        </div>
      </div>

      <div className="run-route" aria-label="Source and destination">
        <div className="run-sources">
          <span className="metadata-label">Source</span>
          {targets.length ? (
            targets.map((a: Json) => (
              <span className="run-source" key={a.id}>
                {a.platform === "web" ? <GlobeIcon /> : <MobileIcon />}
                <span>
                  {a.app_name}{" "}
                  <span className="secondary">{platformName(a.platform)}</span>{" "}
                  <code>{a.embrace_app_id}</code>
                </span>
              </span>
            ))
          ) : (
            <span>—</span>
          )}
        </div>
        <div
          className={"traffic-motion " + (trafficActive ? "is-active" : "")}
          role="img"
          aria-label={
            trafficActive ? "Generator active" : "Generator activity idle"
          }
          title="Generator activity; delivery results appear below"
        >
          <span />
          <span />
          <span />
        </div>
        <div className="run-destination">
          <span className="metadata-label">Destination</span>
          <span>
            {run?.dry_run
              ? "Local output only"
              : spec?.destinations?.mode === "end_to_end"
                ? `Embrace + ${spec.chronosphere?.tenant || "Cortex XCOR"}`
                : spec
                  ? "Embrace"
                  : "—"}
          </span>
        </div>
      </div>

      {!run && fetchError && (
        <div className="run-notice" role="alert">
          <strong>Run details are unavailable.</strong> {fetchError.message}
        </div>
      )}
      {uncertain && (
        <div className="run-notice" role="alert">
          <strong>
            {stale
              ? "Worker updates are delayed."
              : "Showing the last received run data."}
          </strong>{" "}
          {active
            ? "The run may still be active."
            : "The displayed result has not been refreshed."}{" "}
          {updatedAt
            ? `Last received ${receiptTime(updatedAt)}.`
            : "Waiting for an update."}
          {stale
            ? " Check the server before starting another run."
            : " Updates reconnect automatically."}
        </div>
      )}
      {run?.status === "failed" && (
        <div className="run-notice" role="alert">
          <strong>The run needs attention.</strong> Some data may have been
          delivered. Review the receipts and server log before sending again.
        </div>
      )}
      {fetchError instanceof ApiError && fetchError.status === 404 && (
        <Button onClick={again}>Return to setup</Button>
      )}

      <dl className="run-metrics" aria-label="Run metrics">
        <div>
          <dt>Sessions generated</dt>
          <dd>{number(counters?.sessions ?? run?.sessions ?? undefined)}</dd>
        </div>
        <div>
          <dt>
            {countUnit} {run?.dry_run ? "generated" : "acknowledged"}
          </dt>
          <dd>{number(recordCount ?? payloadCount)}</dd>
        </div>
        <div>
          <dt>Delivery errors</dt>
          <dd className={counters?.errors ? "danger" : ""}>
            {number(counters?.errors ?? undefined)}
          </dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{elapsed === null ? "—" : clock(elapsed)}</dd>
        </div>
      </dl>
      <p className="metrics-note">
        {run?.dry_run
          ? "Local test · nothing is sent."
          : "API responses · dashboard visibility is separate."}
      </p>

      {run?.mode === "live" && (
        <section className="run-schedule" aria-label="Sending schedule">
          <div className="schedule-line">
            <div>
              <h2>Schedule</h2>
              <span>
                {spec?.traffic?.rate_per_minute !== undefined
                  ? `${spec.traffic.rate_per_minute} sessions/min`
                  : "Rate unavailable"}
              </span>
              <span>
                {duration
                  ? `${duration}-minute limit`
                  : spec
                    ? "No time limit"
                    : "Duration unavailable"}
              </span>
            </div>
            <span className="schedule-remaining">
              {run.ended
                ? "Run ended"
                : duration && remaining !== null
                  ? remaining
                    ? `${clock(remaining)} remaining`
                    : "Time limit reached"
                  : ""}
            </span>
          </div>
          {!!duration && elapsed !== null && (
            <progress
              aria-label="Scheduled time elapsed"
              value={Math.min(elapsed, duration * 60)}
              max={duration * 60}
            />
          )}
          <div className="schedule-timestamps">
            <span>
              Started {time(run.started)}
              {duration && run.started
                ? ` · Scheduled end ${time(run.started + duration * 60)}`
                : ""}
            </span>
            {run.ended && <span>Ended {time(run.ended)}</span>}
          </div>
          {!run.ended && (
            <p className="hint">
              {draining
                ? run.desired_status === "paused"
                  ? "Finishing in-flight sessions before pausing."
                  : "Finishing pending deliveries before stopping."
                : run.status === "paused"
                  ? duration
                    ? "Pausing does not extend the scheduled end time."
                    : "Generation is paused. Resume to continue sending."
                  : duration
                    ? "Time limit · pending deliveries may finish later."
                    : "Sending continues until paused or stopped."}
            </p>
          )}
        </section>
      )}

      <section className="receipt-section" aria-labelledby="receipts-title">
        <div className="receipt-heading">
          <h2 id="receipts-title">Recent delivery receipts</h2>
          <span>
            {receipts.length ? `Last ${Math.min(20, receipts.length)}` : ""}
          </span>
        </div>
        {receipts.length ? (
          <div
            className="table-scroll"
            tabIndex={0}
            aria-label="Delivery receipts"
          >
            <table>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Destination</th>
                  <th scope="col">Type</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">HTTP</th>
                  <th scope="col" className="numeric">
                    Records
                  </th>
                  <th scope="col" className="numeric">
                    Rejected
                  </th>
                  <th scope="col">
                    <span className="sr-only">Details</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {receipts.slice(0, 20).map((r) => (
                  <tr
                    key={r.id}
                    className={
                      !["accepted", "expired"].includes(r.status)
                        ? "receipt-problem"
                        : ""
                    }
                  >
                    <td className="receipt-time">{receiptTime(r.time_ms)}</td>
                    <td>
                      {r.target === "embrace"
                        ? "Embrace"
                        : r.target === "otlp"
                          ? "Cortex XCOR"
                          : r.target}
                    </td>
                    <td>{r.kind}</td>
                    <td>
                      <span
                        className={`receipt-status ${r.status === "accepted" ? "positive" : r.status === "expired" ? "neutral" : "danger"}`}
                      >
                        <i aria-hidden="true" />
                        {r.status === "accepted"
                          ? run?.dry_run
                            ? "Generated"
                            : "Acknowledged"
                          : r.status}
                      </span>
                    </td>
                    <td className="receipt-http">
                      {r.http_status ?? (run?.dry_run ? "Local" : "—")}
                    </td>
                    <td className="numeric">
                      {number(r.accepted ?? undefined)}
                    </td>
                    <td
                      className={`numeric ${r.rejected ? "danger" : "secondary"}`}
                    >
                      {number(r.rejected ?? undefined)}
                    </td>
                    <td>
                      <button
                        className="icon-button receipt-details"
                        aria-label={`Inspect delivery receipt ${r.id}`}
                        onClick={() => setReceipt(r)}
                      >
                        <ChevronRightIcon />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-receipts">
            <h3>
              {run
                ? "No delivery receipts yet"
                : fetchError
                  ? "Delivery receipts unavailable"
                  : "Loading delivery receipts"}
            </h3>
            <p>
              {run
                ? "Receipts appear after the first upload attempt. Sessions may still be in flight."
                : fetchError
                  ? "Run details could not be retrieved from the server."
                  : "Waiting for the server’s run details."}
            </p>
          </div>
        )}
      </section>
      <Modal
        open={!!receipt}
        onOpenChange={(v) => {
          if (!v) setReceipt(null);
        }}
        title="Delivery receipt"
        description="The recorded response for this upload attempt. An HTTP acknowledgement does not verify dashboard ingestion."
      >
        <pre>{pretty(receipt)}</pre>
      </Modal>
    </div>
  );
}
