# Owner actions and live acceptance

Local implementation, payload tests, protocol tests and container tests do not establish tenant retention or dashboard visibility. The actions below remain unperformed. They require the owner's approval under the handoff. The separate `~/xcor-demo-generator` checkout and its LaunchAgent remain untouched.

## 1. Configure the two existing apps and retention

| System | Exact configuration to check or apply |
|---|---|
| Cortex XCOR RUM | Android `5nrc2`, app name `Vantra`, production; web `kfs9f`, app name `Vantra Web`, production. Keep separate app IDs. |
| Network span forwarding | Include both apps with destination tenant `demo-sandbox`. Allow `api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`; enable the organization's trace-link feature. |
| Web Origins | Default is no Origin header. If enforcement is enabled, choose `site` and allow `https://www.vantra.com` and `https://app.vantra.com`, or choose one explicitly allowed Origin. |
| Cortex XCOR | Extend `vantra-end-to-end` with `vantra_web-web-production` alongside `vantra-android-production`, `shop-experience-api`, `banking-experience-api`, `travel-experience-api`. Preserve unrelated dataset assignments and order; use managed Allow retention. |
| App name | If web is renamed to `Vantra`, update its target app name and use `vantra-web-production` instead. Do not use both names interchangeably. |
| Dashboards | Import/review the User Flows and saved filters from Connections → setup kit; apply the exact Page Group rules exported there. |
| Access | Store a trace ingest/query token and an Embrace service-account token in Settings, or use read-only Embrace MCP access for verification. Tokens stay server-side. |

Wait at least five minutes after forwarding changes. Record observed evidence in Connections. No tenant-configuration write is automated: the owner reviews and applies the exact setup kit through the platform controls.

## 2. Run the forwarding probe first

All commands below run from `~/Session-Generator-V2` on branch `v2`. First inspect local output:

```sh
python3 generate.py run spec/examples/forwarding-probe.json --dry-run out/probe-review --quiet --seed 7331
python3 tools/check_output.py out/probe-review
```

After approval to send the six probe sessions:

```sh
export CHRONOSPHERE_API_TOKEN_FILE="$HOME/Library/Application Support/Session-Generator/chronosphere.token"
python3 generate.py run spec/examples/forwarding-probe.json --quiet --seed 7331
```

The command prints its `.runs/RUN_ID/manifest.jsonl` path. Wait five minutes, then query every known ID (read-only):

```sh
.venv/bin/python tools/verify_v2.py spec/examples/forwarding-probe.json .runs/RUN_ID/manifest.jsonl --out out/probe-verification.json
```

Require each expected trace to have exactly one root with the correct forwarded app service and no missing parents. Inspect each app's network request in Cortex XCOR RUM: it must say **Forwarded**, expose **View trace in Cortex XCOR**, and open that complete trace. Stop here if forwarding or retention fails; HTTP 200 is not proof. The server alternative is Connections → Run forwarding probe, followed by Runs → Verify run.

## 3. Approve and run the 30-minute acceptance cohort

`spec/examples/acceptance-live.json` requests Android and web, 12 journeys/minute for 30 minutes (about 360 journeys before stochastic rate variation), native crash/freeze faults, retail JS exceptions and mobile Product LCP p75 of 3800 ms. Review the spec and local preview first. After separate approval for this live volume:

```sh
python3 generate.py run spec/examples/acceptance-live.json --quiet --seed 7331
# After forwarding settles, substitute the printed RUN_ID:
.venv/bin/python tools/verify_v2.py spec/examples/acceptance-live.json .runs/RUN_ID/manifest.jsonl --out out/acceptance-verification.json
```

The local emitted report is `.runs/RUN_ID/reports/emitted.json`. Required evidence:

- Requested/emitted fault shares within ±2 percentage points; retain per-platform and per-business-unit denominators. Tiny filtered cohorts need a longer approved run.
- At least 95% of eligible known traces complete for **both** app services, with the managed Allow dataset and retention rate 1. Missing IDs remain in the denominator.
- Embrace sessions, native crashes, network domains and named root spans visible in the rolling range.
- Web p75 by browser, device and surface within ±10% of target for sufficiently populated segments. Schedule the selected regression in Studio and verify its onset/recovery in the time series.
- Stable JS issue grouping; Page Load Insights shows Chrome desktop page-load p95 and resources; Pages health shows views, LCP and failures per route template.
- Dashboard network links open the correct full backend traces.

Use Embrace MCP `get_session_distribution` with the app ID and `time_window=last_24_hours` (`group_by=app_version`), `list_network_domains`, `list_root_spans`, `get_web_vital_distribution` (`name=LCP`, then other uppercase vitals; `dimension=browser_name`, `device_type`, `surface_label`), `get_web_vital_timeseries` (`name=LCP`), and `list_exceptions`. Inspect each connected tool's current schema; hourly distribution is not a supported group-by. Obtain hourly user-session counts through the platform export/API, then attach `{source,session_unit:"user_sessions",hourly_counts,vitals,exception_groups}` to the run's observations endpoint. The server discovers remote MCP schemas rather than assuming parameter names.

## 4. Backfill and mode transitions

In Studio → Plan preparation, review a sessions-only 24-hour plan at the desired rate and explicitly approve it. Verify hourly counts and vitals in **Last 24 hours**. It must contain no traceparents or backend exports. Historic sessions do not backfill alerts, metric exports or version-comparison views.

Separately approve end-to-end preparation: history → fresh eight-minute burst → live stream. Verify no trace links on history older than eight minutes and complete traces on eligible recent requests. Changing a running scenario's destination applies only to newly started sessions; record the displayed first trace time. Do not resend history to repair configuration: it double-counts sessions.

## 5. Optional connected capabilities

- **Copilot:** add an Anthropic API key in Settings and check access to `claude-sonnet-5-5` or the selected `claude-opus-5-5`. Recorded protocol tests passed locally; no paid model call occurred. Review each proposal before Apply.
- **iOS:** create a production iOS app, replace `ios00`, add its derived forwarded service/domain configuration, and repeat the small probe. Never reuse Android or web app IDs.
- **RED metrics:** separately approve `spec/examples/backend-red-metrics.json`, then inspect the three service RED metrics. Check export receipts for expired or partially rejected points. No metric retention claim follows from HTTP status.
- **Source maps:** follow [LATER-FEATURES](LATER-FEATURES.md), review release assets, add the symbol token and explicitly approve upload **before** emitting symbolicated exceptions.
- **Regional hosting:** provision actual regional hosts and follow the reviewed plan/apply workflow in [LATER-FEATURES](LATER-FEATURES.md). Labels cannot create geographic diversity.
- **Always-on cut-over:** follow [HOSTING](HOSTING.md). Approve draining/stopping the existing LaunchAgent only after the new stream's forwarding and health are proven. No restart/migration has been performed.
