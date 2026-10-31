import { useState } from "react";
import {
  MagnifyingGlassIcon,
  ReloadIcon,
  CopyIcon,
  CheckIcon,
} from "@radix-ui/react-icons";
import { Json, number, pretty } from "./api";
import { Badge, Button, Modal, Panel } from "./ui";

const attributes = (row: Json) =>
  Array.isArray(row.attributes)
    ? Object.fromEntries(row.attributes.map((a: Json) => [a.key, a.value]))
    : row.attributes || {};
const ms = (row: Json) =>
  Math.max(0, (Number(row.end) - Number(row.start)) / 1e6);
const colors = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
];
const hasError = (row: Json) => {
  const a = attributes(row);
  return (
    !!a["error.type"] ||
    !!a["emb.error_code"] ||
    Number(a["http.status_code"] || a["http.response.status_code"]) >= 400
  );
};
export default function PreviewStep({
  data,
  busy,
  refresh,
}: {
  data: Json | null;
  busy: boolean;
  refresh: () => void;
}) {
  const [platform, setPlatform] = useState("");
  const [detail, setDetail] = useState<Json | null>(null);
  const [eventFilter, setEventFilter] = useState("");
  const [traceFilter, setTraceFilter] = useState("");
  const [copied, setCopied] = useState("");
  if (!data)
    return (
      <Panel>
        <div className="preview-loading">
          <span className="spinner" />
          <h2>Building your preview</h2>
          <p>
            Generating real session and trace payloads locally. Nothing is sent.
          </p>
        </div>
      </Panel>
    );
  const platforms = Object.keys(data.timelines);
  const selected = platforms.includes(platform) ? platform : platforms[0];
  const rows = (data.timelines[selected] || [])
    .slice()
    .sort((a: Json, b: Json) => Number(a.start) - Number(b.start));
  const first = Number(rows[0]?.start) || 0;
  const trace: Json[] = data.trace_waterfall || [];
  const start = Math.min(...trace.map((r) => Number(r.start)));
  const duration = Math.max(
    1,
    Math.max(...trace.map((r) => Number(r.end))) - start,
  );
  const services = [...new Set(trace.map((r) => r.service))];
  const serviceColor = (service: string) =>
    colors[services.indexOf(service) % colors.length];
  const events = rows.filter((r: Json) =>
    r.name.toLowerCase().includes(eventFilter.toLowerCase()),
  );
  const spans = trace.filter((r) =>
    `${r.service} ${r.name}`.toLowerCase().includes(traceFilter.toLowerCase()),
  );
  const inspect = (row: Json) => {
    setCopied("");
    setDetail(row);
  };
  return (
    <>
      <div className="preview-stats">
        <div>
          <strong>{number(data.sample_count)}</strong>
          <span>Preview sessions</span>
        </div>
        <div>
          <strong>{number(data.report.sessions_with_traces)}</strong>
          <span>Sessions with traces</span>
        </div>
        <div>
          <strong>{trace.length}</strong>
          <span>Spans in sample trace</span>
        </div>
        <div>
          <strong className="mint">
            {data.linter.length ? "Review" : "Passed"}
          </strong>
          <span>Payload validation</span>
        </div>
      </div>
      <div className="section-toolbar">
        <p>This is a local sample of the data you’re about to generate.</p>
        <Button onClick={refresh} disabled={busy}>
          <ReloadIcon />
          {busy ? "Refreshing…" : "Refresh preview"}
        </Button>
      </div>
      {!!data.report.faults.length && (
        <Panel title="Included signals">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Signal</th>
                  <th>Requested</th>
                  <th>In this sample</th>
                  <th>Affected / eligible</th>
                </tr>
              </thead>
              <tbody>
                {data.report.faults.map((f: Json) => (
                  <tr key={f.id}>
                    <td>{f.id.replaceAll("-", " ")}</td>
                    <td>
                      {f.requested_pct == null
                        ? "—"
                        : f.requested_pct.toFixed(1) + "%"}
                    </td>
                    <td>
                      <div className="sample-share">
                        <span>
                          {f.emitted_pct == null
                            ? "Not measurable"
                            : f.emitted_pct.toFixed(1) + "%"}
                        </span>
                        {f.emitted_pct != null && (
                          <span className="share-track" aria-hidden="true">
                            <i
                              style={{
                                width: `${Math.max(0, Math.min(100, f.emitted_pct))}%`,
                              }}
                            />
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      {f.emitted_sessions} / {f.eligible}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">
            Rates apply to eligible sessions. Small previews can differ from the
            full run.
          </p>
        </Panel>
      )}
      <Panel
        title="Session timeline"
        action={
          <div className="tabs" role="group" aria-label="Preview platform">
            {platforms.map((p) => (
              <button
                key={p}
                className={p === selected ? "selected" : ""}
                onClick={() => setPlatform(p)}
              >
                {p === "ios" ? "iOS" : p === "web" ? "Web" : "Android"}
              </button>
            ))}
          </div>
        }
      >
        <div className="data-toolbar">
          <label className="search-control">
            <MagnifyingGlassIcon aria-hidden="true" />
            <input
              type="search"
              aria-label="Filter session events"
              placeholder="Filter events…"
              value={eventFilter}
              onChange={(e) => setEventFilter(e.target.value)}
            />
          </label>
          <span>{number(events.length)} events</span>
        </div>
        <div className="timeline">
          {events.map((r: Json, i: number) => (
            <button
              key={i}
              className={"timeline-event" + (hasError(r) ? " event-error" : "")}
              onClick={() => inspect(r)}
            >
              <span className="timeline-time">
                {((Number(r.start) - first) / 1e9).toFixed(2)}s
              </span>
              <span className="timeline-dot" />
              <strong>{r.name}</strong>
              <span className="event-duration">
                {((Number(r.end) - Number(r.start)) / 1e6).toFixed(1)} ms
              </span>
            </button>
          ))}
          {!events.length && (
            <p className="empty-data">No events match this filter.</p>
          )}
        </div>
      </Panel>
      <Panel
        title="Distributed trace"
        action={
          <Badge>
            {services.length} services · {trace.length} spans
          </Badge>
        }
      >
        {trace.length ? (
          <>
            <div className="data-toolbar">
              <label className="search-control">
                <MagnifyingGlassIcon aria-hidden="true" />
                <input
                  type="search"
                  aria-label="Filter trace spans"
                  placeholder="Filter service or operation…"
                  value={traceFilter}
                  onChange={(e) => setTraceFilter(e.target.value)}
                />
              </label>
              <span>{(duration / 1e6).toFixed(1)} ms total</span>
            </div>
            <div className="waterfall">
              <div className="waterfall-header">
                <span>Service / operation</span>
                <span className="waterfall-axis">
                  <span>0</span>
                  <span>{(duration / 2e6).toFixed(0)} ms</span>
                  <span>{(duration / 1e6).toFixed(0)} ms</span>
                </span>
                <span>Duration</span>
              </div>
              {spans.map((r: Json, i: number) => (
                <button
                  key={i}
                  className={
                    "waterfall-row" + (hasError(r) ? " event-error" : "")
                  }
                  onClick={() => inspect(r)}
                >
                  <div>
                    <strong>
                      <i
                        className="service-dot"
                        style={{ background: serviceColor(r.service) }}
                      />
                      {r.service}
                    </strong>
                    <span>{r.name}</span>
                  </div>
                  <div className="waterfall-track">
                    <i
                      style={{
                        left:
                          Math.max(
                            0,
                            ((Number(r.start) - start) / duration) * 100,
                          ) + "%",
                        width:
                          Math.max(
                            0.6,
                            ((Number(r.end) - Number(r.start)) / duration) *
                              100,
                          ) + "%",
                        background: serviceColor(r.service),
                      }}
                    />
                  </div>
                  <span>
                    {((Number(r.end) - Number(r.start)) / 1e6).toFixed(1)} ms
                  </span>
                </button>
              ))}
              {!spans.length && (
                <p className="empty-data">No spans match this filter.</p>
              )}
            </div>
          </>
        ) : (
          <p className="hint">
            This destination includes sessions only. Select sessions and
            distributed traces in step 1 to include a backend trace.
          </p>
        )}
      </Panel>
      {data.report.web_vitals?.length > 0 && (
        <details className="panel">
          <summary>Web performance measurements</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Page</th>
                  <th>Browser / device</th>
                  <th>Vital</th>
                  <th>p75</th>
                </tr>
              </thead>
              <tbody>
                {data.report.web_vitals
                  .slice(0, 30)
                  .map((v: Json, i: number) => (
                    <tr key={i}>
                      <td>{v.page_group}</td>
                      <td>
                        {v.browser_family} · {v.device_type}
                      </td>
                      <td>{String(v.metric).toUpperCase()}</td>
                      <td>
                        {Number(v.p75).toFixed(v.metric === "cls" ? 3 : 0)}
                        {v.metric === "cls" ? "" : " ms"}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {data.exception && (
        <details className="panel exception-preview">
          <summary>
            JavaScript exception{" "}
            <span className="secondary">
              {data.exception["exception.name"] ||
                data.exception["exception.type"]}
            </span>
          </summary>
          <p>{data.exception["exception.message"]}</p>
          <pre>
            {data.exception["exception.stacktrace"] ||
              "This browser did not attach an application stack."}
          </pre>
        </details>
      )}
      {data.linter.length > 0 && (
        <Panel title="Validation notes">
          {data.linter.map((v: Json, i: number) => (
            <p key={i}>{v.message}</p>
          ))}
        </Panel>
      )}
      <Modal
        open={!!detail}
        onOpenChange={(open) => !open && setDetail(null)}
        title={detail?.name || "Payload details"}
        description="Values from the generated sample."
      >
        {detail && (
          <>
            <dl className="inspector-grid">
              <div>
                <dt>Duration</dt>
                <dd>{ms(detail).toFixed(2)} ms</dd>
              </div>
              {detail.service && (
                <div>
                  <dt>Service</dt>
                  <dd>{detail.service}</dd>
                </div>
              )}
              {detail.id && (
                <div>
                  <dt>Span ID</dt>
                  <dd>
                    <code>{detail.id}</code>
                  </dd>
                </div>
              )}
              {detail.trace_id && (
                <div>
                  <dt>Trace ID</dt>
                  <dd>
                    <code>{detail.trace_id}</code>
                  </dd>
                </div>
              )}
            </dl>
            <dl className="attribute-list">
              {Object.entries(attributes(detail)).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>
                    {typeof value === "object"
                      ? JSON.stringify(value)
                      : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
            <div className="inspector-actions">
              <Button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(pretty(detail));
                    setCopied("Copied");
                  } catch {
                    setCopied("Copy unavailable. Use the raw payload below.");
                  }
                }}
              >
                {copied === "Copied" ? <CheckIcon /> : <CopyIcon />}Copy payload
              </Button>
              <span role="status">{copied}</span>
            </div>
            <details className="raw-payload">
              <summary>Raw payload</summary>
              <pre>{pretty(detail)}</pre>
            </details>
          </>
        )}
      </Modal>
    </>
  );
}
