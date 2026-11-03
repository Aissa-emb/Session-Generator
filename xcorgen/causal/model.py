"""Relative monotonic graph; IDs come from a separate emission namespace."""
import hashlib
import random
import re
from dataclasses import asdict, dataclass, field
from .registry import VERSION, SERVICES, SCENARIOS, HTTP_OWNERS, RPC_OWNERS


def stream(seed, index, purpose):
    return random.Random(int(hashlib.sha256(f"{VERSION}:{seed}:{index}:{purpose}".encode()).hexdigest(), 16))


def ident(namespace, index, label, size=16):
    return hashlib.sha256(f"{namespace}:{index}:{label}".encode()).hexdigest()[:size * 2]


@dataclass
class Span:
    key: str
    service: str
    name: str
    kind: int
    start: float
    end: float
    parent: str | None = None
    trace: str = "request"
    attributes: dict = field(default_factory=dict)
    error: str | None = None
    dependencies: list = field(default_factory=list)
    links: list = field(default_factory=list)
    async_work: bool = False
    diagnostic: str | None = None


@dataclass
class Request:
    key: str
    route: str
    client: str
    status: int | None
    transport: str | None = None
    response: dict = field(default_factory=dict)
    operation: str = ""
    expected_absence: bool = False


@dataclass
class Event:
    at: float
    kind: str
    name: str
    data: dict = field(default_factory=dict)


@dataclass
class Execution:
    scenario: str
    variant: str
    platform: str
    seed: int
    index: int
    namespace: str
    config: dict
    spans: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    events: list = field(default_factory=list)
    outcome: str = "completed"
    causal_node: str | None = None
    effects: dict = field(default_factory=dict)
    final_ms: float = 0

    @property
    def domain(self):
        return SCENARIOS[self.scenario][1]

    def span_id(self, key):
        return ident(self.namespace, self.index, "span:" + key, 8)

    def trace_id(self, trace):
        return ident(self.namespace, self.index, "trace:" + trace)

    def operation_id(self, name="operation"):
        return ident(self.namespace, self.index, name)

    def add(self, key, service, name, kind, start, end, parent=None, **kw):
        node = Span(key, service, name, kind, start, end, parent, **kw)
        self.spans.append(node)
        return node

    def event(self, at, kind, name, **data):
        self.events.append(Event(at, kind, name, data))

    def get(self, key):
        return next(s for s in self.spans if s.key == key)

    def metadata(self, build):
        return {"demo.synthetic": True, "demo.run_id": self.namespace, "demo.scenario_id": self.scenario,
            "demo.scenario_version": VERSION, "demo.journey": SCENARIOS[self.scenario][2],
            "demo.variant": self.variant, "demo.client_platform": self.platform, "demo.client_build": build,
            "demo.operation_id": self.operation_id(), "demo.feature.detail_optional": self.variant != "affected",
            "demo.feature.transfer_view": self.variant == "affected" and self.scenario == "frontend-regression",
            "demo.network.profile": self.config.get("network", "wifi"),
            "demo.cohort.region": self.config.get("region", "us-east-1"),
            "demo.cohort.release": self.config.get("service_release", "2026.10.2") if self.variant == 'affected' else '2026.10.1'}

    def manifest(self):
        return {"version": VERSION, "scenario": self.scenario, "variant": self.variant,
            "platform": self.platform, "seed": self.seed, "session_index": self.index,
            "emission_namespace": self.namespace, "expected_outcome": self.outcome,
            "causal_node": self.causal_node, "causal_span_id": self.span_id(self.causal_node) if self.causal_node else None,
            "effects": self.effects, "config": self.config, "events": [asdict(e) for e in self.events],
            "lifecycle": {"process_id": self.operation_id('process'), "launch": "new_process",
                "terminates_fatally": self.outcome == 'process_terminated', "relaunch": None},
            "graph": [dict(asdict(s), span_id=self.span_id(s.key), trace_id=self.trace_id(s.trace)) for s in self.spans],
            "requests": [dict(asdict(r), trace_id=self.trace_id(self.get(r.client).trace),
                client_span_id=self.span_id(r.client)) for r in self.requests],
            "evidence": {"implemented": True, "fixture_validated": True, "runtime_executed": False,
                "export_accepted": False, "dashboard_verified": False, "session_url": None,
                "navigation": "unverified: requires retrieved forwarded span emb.dashboard_session"}}

    def validate(self):
        errors = []
        nodes = {s.key: s for s in self.spans}
        if len(nodes) != len(self.spans): errors.append("duplicate span key")
        for s in self.spans:
            if s.service != "frontend" and s.service not in SERVICES: errors.append(f"unknown service {s.service}")
            elif s.service != "frontend" and SERVICES[s.service].domain not in ("shared", self.domain): errors.append("cross-domain service")
            if s.start < 0 or s.end < s.start: errors.append(f"invalid interval {s.key}")
            if not re.fullmatch(r"[0-9a-f]{16}", self.span_id(s.key)): errors.append("invalid ID")
            seen = {s.key}; p = s.parent
            while p:
                if p in seen: errors.append("parent cycle"); break
                if p not in nodes: errors.append(f"missing parent {p}"); break
                seen.add(p); p = nodes[p].parent
            if s.parent in nodes:
                p = nodes[s.parent]
                if p.trace != s.trace: errors.append("cross-trace parent")
                if not s.async_work and (s.start < p.start or s.end > p.end): errors.append(f"synchronous containment {s.key}")
            for dep in s.dependencies:
                if dep not in nodes or nodes[dep].end > s.start: errors.append(f"dependency {s.key}:{dep}")
            for link in s.links:
                if link not in nodes: errors.append("unresolved span link")
            a = s.attributes
            code = a.get("http.response.status_code")
            if code == 0: errors.append("unobserved HTTP status 0")
            if code and code >= 500 and not s.error: errors.append("5xx must fail")
            if code and code < 400 and s.error and s.kind in (2, 3): errors.append("successful HTTP marked error")
            if s.kind == 3 and "http.route" in a: errors.append("server route on client")
            if a.get('db.system.name') and s.kind != 3: errors.append('invented database server')
            if s.kind == 2 and 'http.route' in a and HTTP_OWNERS.get(a['http.route']) != s.service:
                errors.append('HTTP route ownership')
            if s.kind == 2 and 'rpc.service' in a and RPC_OWNERS.get((a['rpc.service'], a.get('rpc.method'))) != s.service:
                errors.append('RPC ownership')
            if s.kind == 2 and s.parent in nodes:
                p=nodes[s.parent]; pa=p.attributes
                if p.kind != 3: errors.append('SERVER without CLIENT boundary')
                if p.service != 'frontend':
                    for key in ('http.request.method', 'rpc.system', 'rpc.service', 'rpc.method', 'rpc.grpc.status_code', 'http.response.status_code'):
                        if a.get(key) != pa.get(key): errors.append('CLIENT/SERVER protocol mismatch')
                    if 'http.route' in a and not pa.get('url.full', '').endswith(a['http.route']): errors.append('CLIENT/SERVER path mismatch')
        for r in self.requests:
            c = nodes.get(r.client)
            if c is None:
                errors.append('missing frontend client'); continue
            receivers = [s for s in self.spans if s.parent == c.key and s.kind == 2]
            if r.expected_absence and receivers: errors.append("pre-send failure has server")
            if not r.expected_absence and not receivers: errors.append("missing ingress")
            if r.transport and r.status is not None: errors.append("transport has response status")
            if not r.transport and any(s.attributes.get("http.response.status_code") != r.status for s in receivers): errors.append("response mismatch")
        if self.scenario == "signin-outage" and self.variant == "affected":
            if any(s.name.startswith("SELECT") for s in self.spans): errors.append("SQL after pool timeout")
            if any(r.route != "signin" for r in self.requests): errors.append("protected fetch after failed authentication")
        for s in self.spans:
            if s.attributes.get("demo.cache.outcome") == "hit":
                if any(n.parent == s.parent and n.name.startswith("SELECT") for n in self.spans): errors.append("SQL under cache hit")
        if self.effects.get("ledger_transactions", 0) > 1 or self.effects.get("transfer_intents", 0) > 1: errors.append("duplicate transfer effect")
        if self.effects.get("ledger_transactions"):
            if self.effects.get("debit") != -self.effects.get("credit", 0): errors.append("unbalanced ledger")
            commit = self.effects.get("completed_at", 0)
            if any(e.name == "Completed" and e.at < commit for e in self.events): errors.append("UI confirms before completion")
        fatal = next((e for e in self.events if e.kind == "fatal"), None)
        if fatal and any(e.at > fatal.at and e.kind not in ("end",) for e in self.events): errors.append("action after fatal termination")
        if fatal and any(s.service == 'frontend' and s.end > fatal.at for s in self.spans): errors.append('client work after fatal termination')
        for event in self.events:
            dep=event.data.get('dependency')
            if dep and (dep not in nodes or nodes[dep].end > event.at): errors.append('UI outcome before observation')
        if self.effects:
            submit=nodes.get('submit.client')
            if submit and self.effects['reference_created_at'] >= submit.start: errors.append('reference not known before request')
            if self.effects['completed_at'] < nodes['submit.ledger'].end: errors.append('completion before ledger commit')
        if errors: raise ValueError("Invalid causal execution: " + "; ".join(errors))
        return self
