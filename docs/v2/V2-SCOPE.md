# Session Generator V2: scope and feasibility

Prepared 2026-10-30 against `Aissa-emb/Session-Generator` `main` at `db5c59a`. No repository changes were made. The build prompt for an AI agent is `V2-BUILD-PROMPT.md` next to this file. It is the single complete prompt, covering the UI, scenario settings, destinations and backfill, the copilot and MCP access, the Android changes and the web generator.

## Verdict

| Capability | Feasible | Notes |
| --- | --- | --- |
| Web UI to connect an Embrace app ID, a Chronosphere tenant and API token | Yes | Straightforward; secrets stay server-side |
| Catalog of front-end and back-end failure scenarios to pick from | Yes | V1 already implements most of the mechanics (crash families, main-thread blockage, handled exceptions, slow flows, payments pool exhaustion, deadlocks, supplier timeouts, canary rollouts, incident phases) |
| Exact distributions ("1 in 3 sessions crashes", ANR, JS exception, slow root spans) | Yes | Needs a per-session quota allocator and an emitted-versus-requested report; V1 has rate settings and a calibration tool to build on |
| Natural-language control in the UI | Yes | An LLM produces validated changes to a scenario document, never raw telemetry |
| Bring your own agent over MCP | Yes | Expose the same operations as an MCP server; Claude Code, Claude Desktop or any MCP client can drive it |
| 24-hour backfill into Embrace | **Mostly yes** | Sessions, crashes, crash-free rate, network, root spans, logs and web vitals show at their original times in rolling ranges; alerts, metric exports and a few receive-date views do not see the history |
| 24-hour backfill into Chronosphere | **No** (traces); **2 hours** (raw metrics) | Chronosphere drops spans more than 10 minutes old; verified by probe |
| Web sessions for B2C multi-page sites, single-page apps and hybrid sites | Yes | Needs its own Embrace web app; every signal can be reproduced from the Web SDK source and real payloads (see "Web generator findings") |
| Controllable web vitals and JavaScript exceptions | Yes | Target p75 per page group, browser and device; exception messages and stacks per browser engine; per-browser capability rules |
| "Demo in 10 minutes" preparation | Yes, within the limits above | Each run chooses its destinations: Cortex XCOR RUM sessions only (24 hours of history available immediately), or end to end with distributed traces (session history immediately, traces from the start of the run onward) |

## Backfill findings

### Chronosphere (Cortex XCOR)

- **Documented rule** ("Tracing ingest limits"): "If the start time of a span is greater than 10 minutes before or after the current time, Cortex XCOR marks the span as invalid and drops it. The parent trace isn't impacted, provided that the other spans in the trace have valid start times." Drops show in `chrono_trace_dropped_volume_in_bytes_count` and in Trace Optimization, but the OTLP request still returns success.
- **Probe on 2026-10-30 17:31 UTC:** three Vantra travel sessions dated 23 h, 12 h and 2 h ago. Chronosphere accepted all 15 backend trace exports with no partial-success rejections, and stored **0 of 14** traces when queried by trace ID 1 and 4 minutes later. Fresh live traces from the always-on generator in the same tenant were stored normally (control).
- **Metrics** ("Metric limits"): raw data points are accepted up to 2 hours late; points that pass through aggregation rules only a few minutes late; points more than 1 minute in the future are rejected. If V2 adds RED metrics, up to 2 hours of raw history can be backfilled.
- **Consequences:**
  - Backend traces can only exist for roughly the last 10 minutes at send time. There is no supported way to place a trace at 14:00 yesterday.
  - A backfilled Embrace session with a traceparent gets forwarded by Embrace, Chronosphere drops the old span, and the session still shows "View trace in Cortex XCOR" leading to an empty trace. V2 must strip traceparents from requests older than about 8 minutes.
  - V1's burst mode spreads sessions over the last 30 minutes by default, so most of its backend traces are dropped today. Bursts should be limited to the last 8 minutes, or V2 should apply the traceparent rule above.
  - Live streams depend on Embrace forwarding a span within 10 minutes of the request starting. Long session parts (a part is forwarded only when it ends) or an Embrace processing delay push early requests past that window.

### Embrace (Cortex XCOR RUM)

Investigated in the Embrace backend (`~/Claudecode/go`), dashboard API and dashboard on 2026-10-30.

- **Storage:** session, crash, span, network, log and web-vital tables keep the event time (`start`, `start_time`, `time`, rollup `hour` computed from event time) and partition by the receive date (`date`). Example: `session_rollup_mv` uses `toRelativeHourNum(start) AS hour`; `domain/session/row.go:74` stores `Start` clamped to receive minus 7 days.
- **Queries:** every time filter is "receive date between the range's dates AND event time between the range" (`lib/query/sql/helper.go:495-534`, `lib/common/time.go:613-618`), and every chart buckets by event time. "Last 24 hours" ends at the next hour, so its date range covers yesterday and today and always includes rows received today. The session list filters and sorts by session `start` (`service/query/session.go:556-568`).
- **Result:** a session dated 14:00 yesterday and sent now appears at 14:00 yesterday in session counts and lists, crashes and crash-free rate, network endpoints, root spans, logs and web vitals, for "Last 24 hours" and other rolling ranges. Session detail pages load normally.
- **Ingest:** no age-based rejection within 24 hours (clamps start at 7 days for sessions, logs and crashes, 14 days for network requests and JavaScript crashes). User session parts stitch by `emb.user_session_id` plus device ID within ±3 days of receipt, which the generator already sends. Ingest rate limiting would sit in nginx, which is not in these repositories, so large backfills should be throttled and verified.
- **What does not work with backfilled history:**
  1. **Alerts:** metric and network monitors evaluate a window ending now, and new-issue alerts key on first-seen time, so they do not fire for history. An alert in a demo must come from a live incident.
  2. **Metric exports** (Datadog, Grafana, Metrics API) process each window once shortly after it closes and never re-send past windows.
  3. **Custom ranges that end before UTC midnight of the send day** drop the rows, because the receive-date filter is not padded. Present with rolling ranges ("Last 24 hours", "Last 6 hours"), not a calendar "Yesterday".
  4. **Receive-date views:** version-over-version comparisons, per-day version counts, filter-value suggestions and the DAU field treat the data as received today.
  5. **Duplicates:** re-sending a session with the same IDs double-counts session totals (dedupe only counts by default). Every backfill job must be idempotent.
  6. Stitching for older SDKs without user session IDs breaks after about 13 hours; the generator's Android and web payloads carry user session IDs, so this does not apply.
- **Network span forwarding** has no age cutoff (`loader/session/nsf/internal/processor.go:124-154`): Embrace forwards old network spans with their original timestamps, and Chronosphere then drops them (see above). This is why backfilled requests must not carry traceparents.

## Destination modes (decided 2026-10-30)

Because Chronosphere cannot take trace history, every run chooses what it sends:

| Mode | Sends | History | Typical use |
| --- | --- | --- | --- |
| **Cortex XCOR RUM sessions only** | Embrace sessions, crashes, ANRs, logs, network calls. Network requests carry no traceparent, so nothing is forwarded and no "View trace" links appear | Up to 24 hours, available within minutes | Front-end stories: crash spikes, ANRs, release regressions, slow flows |
| **End to end (sessions and distributed traces)** | Everything above plus backend traces in Chronosphere, stitched through network span forwarding | Sessions: up to 24 hours immediately. Traces: from the start of the run onward | Front-end to back-end stories: a failing request in a session opens the backend trace with the root cause |

In end-to-end mode, a backfilled run is split automatically: sessions dated more than ~8 minutes before sending go out as sessions only, and everything from the start of the run onward carries traceparents and backend traces. The UI shows this split on the run timeline and states the caveat in plain terms:

> Cortex XCOR accepts trace data only for the last 10 minutes, so distributed traces cannot be backfilled. Traces are generated from the moment this run starts; sessions dated earlier are sent to Cortex XCOR RUM without trace links. For a fuller trace history, start the run at least two hours before the demo.

The recommended preparation for an end-to-end demo is to start the run, with its 24-hour session backfill, about two hours ahead, so Chronosphere holds two hours of complete traces (around 1,200 sessions' worth at the default rate) and any scheduled incident has played out with traces attached.

## Web generator findings

Investigated on 2026-10-30 in the Embrace Web SDK source (`~/Claudecode/embrace-web-sdk`, 2.31.0), the Embrace backend, dashboard API and dashboard, real SDK payloads, and a dry run of `main`.

**All three site types are buildable** on the existing engine:
- **B2C multi-page sites:** every navigation is a full page load (storefronts, content pages, ad landing pages).
- **Single-page apps:** client-side routing with soft navigations (online banking, booking engines).
- **Hybrid:** server-rendered landing pages that lead into a client-routed flow.

**Already correct in today's web mode:** page loads with resource timings; the five Core Web Vitals for full loads; page views, clicks and breadcrumbs; first-interaction and scroll-depth logs; network calls with traceparents; and the exception record format.

**Missing or wrong today:**
1. **Soft navigations:** no `Soft Navigation` spans, and FCP, LCP, CLS and INP are not re-reported per route change.
2. **Long animation frames:** no `tbd` vital or `loaf_scripts` logs, which explain slow interactions.
3. **Other signals:** no DOM-state, rage-click, user-timing, element-timing or server-timing data.
4. **Multi-page lifecycle:** no session part per page load and no back/forward cache behavior.
5. **Per-browser rules:** browsers report signals they do not support. For example, Safari and Firefox must not report CLS or LoAF.
6. **Exception types:** JavaScript exceptions use Android crash types, such as `NumberFormatException` thrown from a `.js` file. This must be fixed before web data is shown.
7. **Vitals control:** vitals are derived from load timing and cannot be set.
8. **Identity:** the shared Vantra app has no web identity, and web mode still uses three separate brands.

**Platform constraints:**
- **Web app:** web needs its own Embrace web app; `5nrc2` is Android.
- **Origin header:** must be allowed for the app, or omitted. When enforcement is on, payloads with a disallowed Origin are dropped silently.
- **Trace links:** forwarding links only network request spans, so page loads cannot open a trace.
- **Page Load Insights:** defaults to Chrome 107+ desktop, so the browser mix needs that traffic.
- **URLs:** the backend rolls them up, so URL schemes must be designed for it (hyphenated slugs, numeric IDs).
- **Exception grouping:** stable within a release; readable frames need source maps uploaded before the exceptions arrive.

## Live system finding (outside V2, needs attention now)

While checking controls, Chronosphere showed **no forwarded app spans at all between 16:37 and 17:37 UTC on 2026-10-30**: 0 of 186 live traces had the Vantra app span as root, so they show a `<Missing>` root. The generator is healthy (6,134 sessions, no send errors, running `447eb6c`), and a dry run of that code puts a W3C traceparent on every network request, all on the three forwarded domains. The break is after Embrace ingest. To tell which:
1. Open a session from the last hour in Embrace and expand a network request. **"Not Forwarded"** means forwarding is off for the app or domain, or the Chronosphere destination's token or tenant changed (Settings > Integrations). **"Forwarded"** with a missing trace means Embrace forwarded the span more than 10 minutes after the request started (processing lag or long session parts), and Chronosphere dropped it as invalid.
2. Check the Chronosphere `chrono_trace_dropped_volume_in_bytes_count` metric for `vantra-android-production`.

## What V2 adds, in brief

1. **ScenarioSpec**: one versioned JSON document that describes the target, app, population, traffic, client faults, backend faults, incidents and rollouts. The UI, the API, the copilot and MCP clients all edit it; the engine executes it.
2. **Fault catalog with exact distributions**: each fault has a rate (probability or exact quota), filters (line of business, app version, OS, device tier, screen, endpoint) and details. Every run reports requested versus emitted counts computed from the actual payloads.
3. **Preset library**: front-end (release regression crash, ANR on results lists, handled exception storm, slow cold start, flaky network, slow critical flow; web JS exceptions and poor web vitals), back-end (payments pool exhaustion across lines of business, inventory deadlock, supplier timeouts, acquirer latency, fraud model latency, cache outage, deploy regression, rate limiting, async lag) and composite stories.
4. **Server and runs**: FastAPI service wrapping the existing engine, managed live streams and bounded jobs, delivery receipts, verification against Chronosphere (trace IDs) and Embrace (API or MCP).
5. **UI** in Cortex XCOR branding: Connections with readiness checks, Scenario studio, Preview (sample Embrace timeline and Chronosphere waterfall), Copilot panel, Runs and streams, Settings.
6. **Natural language**: a copilot that turns requests into validated spec changes with a reviewable diff, and an MCP server so any agent can list presets, preview, start, adjust and verify runs.
7. **Destination modes and demo preparation**: each run is either sessions only or end to end (see "Destination modes"). Demo preparation runs readiness checks, backfills Embrace history, starts the live stream (with traces in end-to-end mode), and can schedule an incident to begin during the demo.
8. **Hosted always-on streams**: the LaunchAgent stops when the Mac sleeps; a container deployment keeps demo tenants current.
9. **Web generator**: B2C multi-page, single-page and hybrid sites with SDK-faithful lifecycle, controllable web vitals, long-animation-frame attribution, per-engine JavaScript exceptions, per-browser capability rules and a web identity for Vantra, sharing backend incidents with Android.
10. **Dashboard setup kit**: per target, the exact forwarding domains, Chronosphere retention dataset, User Flows, saved session filters, web allowed origins and Page Group rules the data needs, with verification where an API allows it.

## Natural language and MCP design

- **Copilot**: server-side LLM tool loop (Anthropic API; `claude-sonnet-5-5` default, `claude-opus-5-5` optional). Tools are the same functions as the MCP server. The model reads the preset catalog, the schema and the current spec, and returns a JSON Patch plus a summary. The server validates (schema, platform limits, realism linter); the UI shows the diff for Apply or Discard. Running streams pick up the new version at the next session start.
- **MCP server**: official MCP Python SDK, streamable HTTP with per-user bearer tokens and stdio for local use. Tools: `get_capabilities`, targets (`list`, `create`, `test`), presets, spec (`get`, `create`, `patch`, `validate`, `explain`, `preview`), runs (`start`, `stop`, `pause`, `list`, `status`), `schedule_incident`, `verify_run`. Outbound actions require `confirm: true`.
- **Guardrails**: size and rate caps, the realism linter (no "demo", "test", "fake" or similar anywhere in emitted data, naming conventions, exception and language consistency), and user-supplied text treated as data.

Example requests the copilot must handle:
- "Make one in three checkout sessions on 10.28.0 crash after the payment fails, Android 16 only."
- "Add a payments database pool outage from 2:00 to 2:20 pm Eastern that affects shop and travel, then recover."
- "Slow the hotel search root span to a p95 of 4 seconds on budget devices."
- "What is this stream generating right now?"

## Risks and open questions for the owner

- **Hosting and users:** local only, or a server for the team? This decides authentication and secret storage.
- **LLM key:** may the copilot use your Anthropic API key, and which model?
- **Chronosphere configuration permissions:** with a config-capable token, V2 can check and create the dataset and allow behavior that stops tail sampling from discarding demo traces; otherwise it only reports retention.
- **Embrace verification access:** V2 should confirm sessions landed using an Embrace API token or the Embrace MCP server; ingest itself always answers 200.
- **Volume:** a 24-hour backfill at today's live rate is about 14,000 sessions; confirm the demo tenant can take bursts of that size.
- **Geography:** all sessions show the runner's location (Embrace uses the sender IP).

## Sources

- Chronosphere tracing ingest limits: https://docs.chronosphere.io/administer/limits-licensing/limits/trace-limits (redirects to docs-xcor.paloaltonetworks.com)
- Chronosphere metric limits: https://docs-xcor.paloaltonetworks.com/administer/limits-licensing/limits/metric-limits
- Chronosphere Data API (ListTraces): https://docs.chronosphere.io/tooling/api-info/data_definition/operations/ListTraces.md
- Embrace backend: owner's clone `~/Claudecode/go` (file references in the Embrace section)
- Probe data: user sessions `7E73B9A828B4025650995F58FB9B54E3` (23 h), `245E0E376FF56265E19AE2A9846FCFEA` (12 h), `59651E92DF44BCC93E7431832A5C74F0` (2 h) in app `5nrc2`
