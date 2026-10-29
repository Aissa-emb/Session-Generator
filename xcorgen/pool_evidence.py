"""Companion work for a per-instance Hikari pool, in bounded 120 s epochs.

The maintenance connection is outside HikariPool-1. Its table lock blocks
50 already-acquired connections. There is no SQL on the waiting request.
Repeated requests share these exact traces instead of inventing 50 more holders.
"""
import hashlib
import random

from . import backend as b, otlp
from .profiles.platform import P, RDS


def companions(call, req):
    if req is None or req["outcome"] != "payment_db_pool" or not call.trace_build:
        return []
    s = req["plan"].s
    runtime = req["plan"].runtime
    if not hasattr(runtime, "pool_epochs"):
        runtime.pool_epochs = set()
    tb = call.trace_build
    waiting = next(x for x in tb.spans if x[3]["name"] == "HikariPool.getConnection")
    span = waiting[3]
    start = int(span["startTimeUnixNano"]) / 1e6
    end = int(span["endTimeUnixNano"]) / 1e6
    exports = []
    for epoch in range(int(start // 120000), int(end // 120000) + 1):
        occurrence = req["plan"].occurrence
        seed = f"{occurrence}:{epoch}"
        tid = hashlib.sha256(seed.encode()).hexdigest()[:32]
        root_id = hashlib.sha256((seed + ":root").encode()).hexdigest()[:16]
        span.setdefault("links", []).append({"traceId": tid, "spanId": root_id,
                                              "attributes": otlp.attrs({"link.reason": "pool_occupants"})})
        s.plan.evidence[-1].setdefault("companion_trace_ids", []).append(tid)
        if seed in runtime.pool_epochs:
            continue
        runtime.pool_epochs.add(seed)
        pas = P["payment-authorization-service"]
        context = {"incident.occurrence_id": occurrence, "incident.phase": req["plan"].phase,
                   "db.lock.resource": "payments.payment_tokens", "db.lock.cause": "token_rotation_access_exclusive"}
        start_ms, duration = epoch * 120000, 120000
        lock = b.sql(pas, "LOCK", "payment_tokens", "LOCK TABLE payment_tokens IN ACCESS EXCLUSIVE MODE",
                     "payments", RDS.format("payments-prod"), ms=(duration, duration))
        lock.attrs.update({"db.connection.pool.name": "maintenance", "db.lock.mode": "AccessExclusiveLock",
                           "transaction.purpose": "token_encryption_rotation", "transaction.holds_lock": True})
        holders = []
        for i in range(50):
            query = b.sql(pas, "SELECT", "payment_tokens",
                          "SELECT token_ref FROM payment_tokens WHERE customer_id = ? FOR UPDATE",
                          "payments", RDS.format("payments-prod"), ms=(duration, duration), rows=1)
            query.attrs.update({"db.client.connection.pool.name": "HikariPool-1", "db.connection.acquired": True,
                                "pool.connection.slot": i + 1, "db.wait_event": "Lock.relation"})
            holders.append(query)
        root = b.internal(pas, "authorization_worker.batch", ms=(.01, .01), children=[lock] + holders, parallel=True,
                          attrs={"pool.active": 50, "pool.idle": 0, "pool.capacity": 50,
                                 "workload.source": "scheduled_authorizations", "pool.epoch": epoch})
        companion = b.build_trace(root, tid, "", start_ms, random.Random(seed), context)
        old_id = companion.spans[0][3]["spanId"]
        companion.spans[0][3]["spanId"] = root_id
        # Exact epoch boundaries prevent overlapping occupancy in adjacent epochs.
        for _, _, _, record in companion.spans:
            if record.get("parentSpanId") == old_id:
                record["parentSpanId"] = root_id
            record["startTimeUnixNano"] = str(int(start_ms * 1e6))
            record["endTimeUnixNano"] = str(int((start_ms + duration) * 1e6))
        exports.append(companion)
    return exports
