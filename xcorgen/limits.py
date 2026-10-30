"""Verified platform limits. These are properties of Cortex XCOR and Cortex XCOR RUM, not generator settings.

Every value here was verified (see docs/v2/V2-SCOPE.md and AGENTS.md). The
engine enforces them, spec validation reports them, and the server, UI,
copilot and MCP `get_capabilities` expose the same text, so no surface
promises data the platforms drop.
"""

MINUTE_MS = 60_000

# Cortex XCOR "Tracing ingest limits": a span whose start time is more than 10 minutes before or after
# ingest is marked invalid and dropped. The OTLP request still returns success.
TRACE_INGEST_WINDOW_MS = 10 * MINUTE_MS
# Sessions or requests dated earlier than this at send time carry no traceparent and produce no backend
# trace. Two minutes of margin cover Embrace processing and the forwarding delay.
TRACE_ELIGIBLE_MS = 8 * MINUTE_MS
# Cortex XCOR RUM shows sessions dated up to 24 hours back at their original times in rolling ranges.
BACKFILL_MAX_MS = 24 * 60 * MINUTE_MS
# Raw metric points are accepted up to 2 hours late, aggregated points a few minutes, future points
# at most 1 minute ahead.
METRICS_RAW_LATE_MS = 120 * MINUTE_MS
METRICS_FUTURE_MS = 1 * MINUTE_MS
# Network span forwarding configuration changes take about five minutes to apply
# (NETWORK_SPAN_DESTINATIONS_REFRESH_PERIOD); forwarding is decided once, at ingest.
FORWARDING_REFRESH_MS = 5 * MINUTE_MS

# Generator guardrails (section 8.3). Configurable per deployment; these are the defaults.
MAX_SESSIONS_PER_JOB = 50_000
MAX_RATE_PER_STREAM = 600.0
MAX_STREAMS_PER_TARGET = 4
DEFAULT_BACKFILL_PAYLOADS_PER_SECOND = 20.0

TRACE_CAVEAT = ("Cortex XCOR accepts trace data only for the last 10 minutes, so distributed traces cannot be "
                "backfilled. Traces are generated from the moment this run starts; sessions dated earlier are "
                "sent to Cortex XCOR RUM without trace links. For a fuller trace history, start the run at least "
                "two hours before the demo.")

BACKFILL_NOTES = (
    "Present history with rolling ranges such as \"Last 24 hours\", not a calendar \"Yesterday\": custom ranges "
    "that end before UTC midnight of the send day exclude rows received today.",
    "Alerts and new-issue alerts do not fire for history; an alert in a demo must come from live traffic.",
    "Metric exports (Datadog, Grafana, Metrics API) do not include history.",
    "Version-over-version views, per-day version counts and DAU treat history as received today.",
    "Re-sending a session with the same IDs double-counts it; every backfill records its IDs and never re-sends them.",
)

PLATFORM_LIMITS = [
    {"id": "trace_ingest_window", "platform": "cortex_xcor",
     "text": "Spans with a start time more than 10 minutes before or after ingest are dropped. The OTLP request "
             "still returns success.",
     "enforced_as": "Requests older than 8 minutes at send time carry no traceparent and produce no backend trace."},
    {"id": "forwarding_delay", "platform": "cortex_xcor",
     "text": "A forwarded client span is dropped if Cortex XCOR RUM forwards it more than 10 minutes after the "
             "request started; long session parts push early requests past that window.",
     "enforced_as": "Readiness checks measure app-rooted trace completeness on known trace IDs."},
    {"id": "tail_sampling", "platform": "cortex_xcor",
     "text": "App-rooted traces fall into whichever dataset and behavior matches. Without an Allow behavior for "
             "the app services they can be tail-sampled away.",
     "enforced_as": "Readiness checks report retention per line of business and platform."},
    {"id": "metrics_lateness", "platform": "cortex_xcor",
     "text": "Raw metric points are accepted up to 2 hours late, aggregated points only a few minutes late, and "
             "points more than 1 minute in the future are rejected.", "enforced_as": "Metrics are not emitted yet."},
    {"id": "forwarding_requirements", "platform": "cortex_xcor_rum",
     "text": "Only perf.network_request spans are forwarded, only for apps included in network span forwarding "
             "with a Cortex XCOR destination and for domains on the forwarding list. Forwarding is decided once, at "
             "ingest; configuration changes take about five minutes to apply. Web document loads and resource "
             "fetches are never linked to traces.",
     "enforced_as": "The dashboard setup kit lists the exact domains; a probe proves forwarding before large runs."},
    {"id": "trace_link", "platform": "cortex_xcor_rum",
     "text": "\"View trace in Cortex XCOR\" appears only when the request's forwarding outcome is success, the app "
             "has a Cortex XCOR destination and tenant, and the organization has the trace-link feature enabled.",
     "enforced_as": "Sessions-only runs send no traceparent, so no link appears."},
    {"id": "ingest_status", "platform": "cortex_xcor_rum",
     "text": "Ingest returns HTTP 200 for everything, including payloads it discards.",
     "enforced_as": "Runs are verified through the Embrace API or MCP server, never from HTTP status alone."},
    {"id": "location", "platform": "cortex_xcor_rum",
     "text": "Session location comes from the sender's IP address, so one runner shows one location.",
     "enforced_as": "Use runners in several regions for geographic spread."},
    {"id": "web_origin", "platform": "cortex_xcor_rum",
     "text": "Web payloads whose Origin header is not on the app's allowed origins are dropped silently when "
             "enforcement is on.", "enforced_as": "Web targets send no Origin header or one allowed origin."},
    {"id": "web_session_part", "platform": "cortex_xcor_rum",
     "text": "Every web spans payload needs a ux.session_part span; resources whose parent document load is not "
             "in the same payload are dropped; file:// URLs are dropped.",
     "enforced_as": "The web generator and check_output.py enforce payload grouping."},
    {"id": "web_vitals_dedupe", "platform": "cortex_xcor_rum",
     "text": "Web vitals are not deduplicated and surface names are not rolled up.",
     "enforced_as": "Each vital is sent once, as a log; surfaces use route templates."},
    {"id": "page_load_insights", "platform": "cortex_xcor_rum",
     "text": "Page Load Insights defaults to Chrome 107+ on desktop.",
     "enforced_as": "Default browser mixes include Chrome desktop traffic."},
    {"id": "backfill", "platform": "cortex_xcor_rum",
     "text": "Sessions can be backfilled up to 24 hours. " + " ".join(BACKFILL_NOTES),
     "enforced_as": "Backfill windows are at most 24 hours, never in the future, and idempotent."},
]


def trace_eligible(event_ms, send_ms):
    """True when a request dated event_ms, sent at send_ms, can still produce a retained backend trace."""
    return send_ms - event_ms <= TRACE_ELIGIBLE_MS and event_ms - send_ms <= TRACE_INGEST_WINDOW_MS


def forwarded_service_name(app_name, platform, environment="production"):
    """Service name Cortex XCOR RUM gives forwarded client spans (go/lib/common/data_destinations_config.go)."""
    if not app_name:
        return ""
    return f"{app_name.replace(' ', '_')}-{platform}-{environment}".lower()


def capabilities():
    return {"limits": PLATFORM_LIMITS, "trace_caveat": TRACE_CAVEAT, "backfill_notes": list(BACKFILL_NOTES),
            "trace_eligible_minutes": TRACE_ELIGIBLE_MS // MINUTE_MS,
            "trace_ingest_window_minutes": TRACE_INGEST_WINDOW_MS // MINUTE_MS,
            "backfill_max_hours": BACKFILL_MAX_MS // (60 * MINUTE_MS),
            "guardrails": {"max_sessions_per_job": MAX_SESSIONS_PER_JOB, "max_rate_per_stream": MAX_RATE_PER_STREAM,
                           "max_streams_per_target": MAX_STREAMS_PER_TARGET}}
