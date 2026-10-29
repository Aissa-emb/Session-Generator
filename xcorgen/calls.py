"""One API call from the client: outcome, network timing and the backend trace that serves it.

Shared by the web and Android sessions. The client span (browser fetch or
OkHttp request) gets the traceparent's trace and span IDs; the backend root
span is its child, so network span forwarding completes the trace in
Chronosphere.
"""

import random

from . import backend
from .util import hex_id, lat, weighted


class Result:
    def __init__(self, status, outcome, data, trace_id, start, end):
        self.status, self.outcome, self.data = status, outcome, data or {}
        self.trace_id, self.start, self.end = trace_id, start, end

    @property
    def ok(self):
        return 200 <= self.status < 400


class Ctx:
    """What an endpoint builder sees: services, params, the chosen outcome and the visitor."""

    def __init__(self, session, params, outcome):
        self.session, self.params, self.outcome = session, params, outcome
        self.svc = session.profile.services
        self.persona = session.persona
        self.incident = session.incident
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
    outcome = weighted(list(weights.items()), rng)
    root, status, data = ep.build(Ctx(session, params, outcome))

    trace_id, span_id = hex_id(16, rng), hex_id(8, rng)
    host = session.api_host
    rtt = p["rtt"] * rng.uniform(0.85, 1.3)
    first = host not in session.connected_hosts
    session.connected_hosts.add(host)
    dns = lat(5, 25, rng) if first else 0.0
    tcp = rtt * rng.uniform(0.9, 1.1) if first else 0.0
    tls = rtt * rng.uniform(1.0, 1.4) if first else 0.0
    req_start = start + rng.uniform(0.3, 4) + dns + tcp + tls
    tb = backend.build_trace(root, trace_id, span_id, req_start + rtt / 2, rng)
    size = rng.randint(*ep.size) if status < 400 else rng.randint(90, 420)
    resp_start = root.end + rtt / 2
    resp_end = resp_start + size * 8 / (p["mbps"] * 1000) + rng.uniform(0.2, 2.5)
    end = resp_end + rng.uniform(0.4, 3)
    marks = {"dns": dns, "tcp": tcp, "tls": tls, "req_start": req_start, "resp_start": resp_start,
             "resp_end": resp_end, "first": first}
    call = Call(ep, f"https://{host}{ep.path(params)}", status, outcome, data, trace_id, span_id, marks, start, end, tb)
    call.response_bytes = size
    call.request_bytes = rng.randint(180, 1400) if ep.method in ("POST", "PUT", "PATCH") else 0
    return call


# Embrace forwards the client network span only after the session part ends and is processed. Backend spans
# are held until then so both halves of a trace reach Chronosphere together; spans that arrive minutes after
# the rest of their trace fall outside Chronosphere's trace assembly window and are dropped.
FORWARD_DELAY_MS = (70_000, 130_000)


def release_backend(session, part_end, rng=random):
    """Schedule the backend traces held for this session part to go out with Embrace's forwarding."""
    for d in session.deliveries:
        if d.pop("hold", False):
            d["due"] = max(d["due"], part_end + rng.uniform(*FORWARD_DELAY_MS))


def deliver_backend(session, call, rng=random):
    """Queue the backend trace (and logs); held until the session part ends (see release_backend)."""
    tb = call.trace_build
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
