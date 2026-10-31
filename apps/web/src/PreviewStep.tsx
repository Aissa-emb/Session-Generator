import { useState } from "react";
import { Json, number, pretty } from "./api";
import { Badge, Button, Modal, Panel } from "./ui";
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
                      {f.emitted_pct == null
                        ? "Not measurable"
                        : f.emitted_pct.toFixed(1) + "%"}
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
        <div className="timeline">
          {rows.slice(0, 35).map((r: Json, i: number) => (
            <button
              key={i}
              className="timeline-event"
              onClick={() => setDetail(r)}
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
        </div>
      </Panel>
      <Panel
        title="Distributed trace"
        action={<Badge>{trace.length} spans</Badge>}
      >
        {trace.length ? (
          <div className="waterfall">
            <div className="waterfall-header">
              <span>Service / operation</span>
              <span>Time · milliseconds</span>
              <span>Duration</span>
            </div>
            {trace.map((r: Json, i: number) => (
              <button
                key={i}
                className="waterfall-row"
                onClick={() => setDetail(r)}
              >
                <div>
                  <strong>{r.service}</strong>
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
                          ((Number(r.end) - Number(r.start)) / duration) * 100,
                        ) + "%",
                      background: [
                        "var(--chart-1)",
                        "var(--chart-2)",
                        "var(--chart-3)",
                        "var(--chart-4)",
                      ][i % 4],
                    }}
                  />
                </div>
                <span>
                  {((Number(r.end) - Number(r.start)) / 1e6).toFixed(1)} ms
                </span>
              </button>
            ))}
          </div>
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
        <pre>{pretty(detail)}</pre>
      </Modal>
    </>
  );
}
