"""One API call from the client: outcome, network timing and the backend trace that serves it.

Shared by the web and Android sessions. The client span (browser fetch or
OkHttp request) gets the traceparent's trace and span IDs; the backend root
span is its child, so network span forwarding completes the trace in
Chronosphere.
"""

import random

from . import backend
from .util import hex_id, lat, weighted
from .scenarios import REQUEST
from .catalog import REACHED_SERVER


class Result:
    def __init__(self, status, outcome, data, trace_id, start, end):
        self.status, self.outcome, self.data = status, outcome, data or {}
        self.trace_id, self.start, self.end = trace_id, start, end

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 400


class Ctx:
    """What an endpoint builder sees: services, params, the chosen outcome and the visitor."""

    def __init__(self, session, params, outcome):
        self.session, self.params, self.outcome = session, params, outcome
        self.svc = session.profile.services
        self.persona = session.persona
        req = REQUEST.get()
        self.incident = req["incident"] if req else session.incident
        self.user_id = session.user_id

    def edge(self, method, route, path, status, children, ms=(0.6, 2.5), parallel=False):
        """Root server span on the edge gateway, the first hop the client request reaches."""
        gw = self.svc[self.session.profile.edge_service]
        return backend.server(gw, method, route, path, status, ms, children, parallel,
                              client_ip=self.persona["ip"], user_agent=self.session.client_user_agent,
                              extra={"url.scheme": "https", "server.address": self.session.api_host,
                                     "server.port": 443, "network.protocol.version": "2",
                                     "enduser.id": self.user_id})


class Call:
    """Timing and identity of one client request, plus the backend trace built for it."""

    def __init__(self, ep, url, status, outcome, data, trace_id, span_id, marks, start, end, trace_build):
        self.ep, self.url, self.status, self.outcome, self.data = ep, url, status, outcome, data
        self.trace_id, self.span_id = trace_id, span_id
        self.traceparent = f"00-{trace_id}-{span_id}-01"
        self.marks, self.start, self.end, self.trace_build = marks, start, end, trace_build
        self.request_bytes = 0
        self.response_bytes = 0


def simulate(session, key, params, start, rng=random):
    """Pick an outcome, build the backend trace and lay out the network phases of the request."""
    profile, p = session.profile, session.persona
    ep = profile.endpoints[key]
    weights = dict(ep.outcomes)
    for name, w in (profile.incidents.get(session.incident, {}).get(key, {}) or {}).items():
        weights[name] = w
    req = session.plan.request(key, params) if hasattr(session, "plan") else None
    outcome = req["outcome"] if req else weighted(list(weights.items()), rng)
    token = REQUEST.set(req)
    replayed = False
    transport = params.get("_transport")
    # Timeouts and resets happen after the request reached the gateway, so the backend still did the work.
    reached = transport is None or transport in REACHED_SERVER
    try:
        prior = req["plan"].runtime.operations.get(req["plan"].operation_id) if req else None
        if reached and key in ("place_order", "create_booking", "create_transfer") and prior and prior["state"] in ("confirmed", "pending"):
            from .operations import replay
            root, status, data = replay(Ctx(session, params, outcome), prior, ep)
            replayed = True
        elif reached and req and req.get("http_error"):
            from .generic_faults import http_error
            root, status, data = http_error(Ctx(session, params, outcome), ep, key, req["http_error"])
        else:
            root, status, data = ep.build(Ctx(session, params, outcome)) if reached else (None, None, {})
    finally:
        REQUEST.reset(token)
    server_status = status
    if root and params.get("_origin_latency"):
        root.min_ms = max(root.min_ms or 0, params["_origin_latency"])

    trace_id, span_id = hex_id(16, rng), hex_id(8, rng)
    host = session.api_host
    rtt = p["rtt"] * rng.uniform(0.85, 1.3)
    first = host not in session.connected_hosts
    session.connected_hosts.add(host)
    dns = lat(5, 25, rng) if first else 0.0
    tcp = rtt * rng.uniform(0.9, 1.1) if first else 0.0
    tls = rtt * rng.uniform(1.0, 1.4) if first else 0.0
    req_start = start + rng.uniform(0.3, 4) + dns + tcp + tls
    context = req["attributes"] if req else {}
    tb = backend.build_trace(root, trace_id, span_id, req_start + rtt / 2, rng, context) if root else None
    size = (rng.randint(*ep.size) if status < 400 else rng.randint(90, 420)) if root else 0
    resp_start = root.end + rtt / 2 if root else start + (1200 if transport == "dns" else 450)
    resp_end = resp_start + size * 8 / (p["mbps"] * 1000) + rng.uniform(0.2, 2.5)
    end = resp_end + rng.uniform(0.4, 3)
    if transport:
        # The client never receives a response: no status code and no response body.
        status, size = None, 0
        if transport == "timeout":
            end = max(end, req_start + rng.choice((10_000, 15_000, 30_000)) + rng.uniform(0, 40))
        elif transport == "reset":
            end = max(end, resp_start + rng.uniform(1, 30))
    marks = {"dns": dns, "tcp": tcp, "tls": tls, "req_start": req_start, "resp_start": resp_start,
             "resp_end": resp_end, "first": first}
    call = Call(ep, f"https://{host}{ep.path(params)}", status, outcome, data, trace_id, span_id, marks, start, end, tb)
    call.response_bytes = size
    call.request_bytes = rng.randint(180, 1400) if ep.method in ("POST", "PUT", "PATCH") else 0
    call.transport = transport
    call.context = context
    if req and not replayed and key in ("place_order", "create_booking", "create_transfer") and server_status is not None:
        state = "confirmed" if server_status < 400 else "pending" if server_status == 504 and key != "create_transfer" else "not_submitted"
        req["plan"].runtime.operations[req["plan"].operation_id] = dict(data, state=state)
        if len(req["plan"].runtime.operations) > 20000:
            req["plan"].runtime.operations.pop(next(iter(req["plan"].runtime.operations)))
    if hasattr(session, "plan"):
        session.plan.evidence.append({"key": key, "status": status, "trace_id": trace_id, "client_span_id": span_id,
            "method": ep.method, "route": ep.route, "url": call.url,
            "traceparent": call.traceparent, "start": start, "end": end, "part_id": session.part.id,
            "incident": req["incident"], "context": context, "transport": call.transport, "data": data,
            "backend_errors": sum(bool(n[3].get("status", {}).get("code") == 2) for n in tb.spans) if tb else 0,
            "first_backend_span_id": tb.spans[0][3]["spanId"] if tb else None})
    from .pool_evidence import companions
    # Occupant traces are only produced for sessions whose traces Cortex XCOR can still ingest.
    call.companions = companions(call, req) if getattr(session, "traces", True) else []
    return call


# Preserve the measured delivery schedule; earlier ordering experiments were
# confounded by tail sampling. Retention must be verified using known IDs.
FORWARD_DELAY_MS = (3_000, 15_000)


def release_backend(session, part_end, rng=random):
    """Schedule the backend traces held for this session part to go out with Embrace's forwarding."""
    for d in session.deliveries:
        if d.pop("hold", False):
            d["due"] = max(d["due"], part_end + rng.uniform(*FORWARD_DELAY_MS))


def deliver_backend(session, call, rng=random):
    """Queue the backend trace (and logs); held until the session part ends (see release_backend)."""
    tb = call.trace_build
    if tb is None:
        return
    done = max(int(s["endTimeUnixNano"]) for _, _, _, s in tb.spans) / 1e6
    traces = tb.traces_request()
    extra = session.client_resource_spans(call) if session.emit_client_spans else None
    if extra:
        traces["resourceSpans"].append(extra)
    session._deliver(done + rng.uniform(1000, 5000), "otlp", "traces", traces)
    session.deliveries[-1]["hold"] = True
    logs = tb.logs_request()
    if logs:
        session._deliver(done + rng.uniform(1000, 5000), "otlp", "logs", logs)
        session.deliveries[-1]["hold"] = True
    for companion in call.companions:
        done = max(int(s["endTimeUnixNano"]) for _, _, _, s in companion.spans) / 1e6
        session._deliver(done + 1000, "otlp", "traces", companion.traces_request())
        session.deliveries[-1]["hold"] = True
