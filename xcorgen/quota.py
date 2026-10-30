"""Outcome allocation: exact quotas and per-session probabilities.

Exact allocation (bursts and backfills) sees every planned session before any
is generated. Each fault's count is fixed to round(sum of its rate over the
eligible sessions), then placed by systematic sampling over the cumulative
rate in time order, with sessions shuffled inside short time buckets. Counts
therefore match within one session, follow time-varying incident curves, and
show no periodic pattern.

Live streams cannot see the future, so StreamingQuota keeps the same
guarantee at block boundaries: each block of eligible sessions receives the
whole number of outcomes its cumulative rate has earned, at random positions.
"""
import math
import random

BUCKET_MS = 5 * 60_000


def _systematic(weights, count, rng):
    """Pick `count` indices with probability proportional to weights (each weight <= 1 after scaling)."""
    total = sum(weights)
    if count <= 0 or total <= 0:
        return []
    step = total / count
    u = rng.uniform(0, step)
    chosen, acc, k = [], 0.0, 0
    for i, w in enumerate(weights):
        acc += w
        while k < count and u + k * step < acc:
            chosen.append(i)
            k += 1
    # Floating-point slack at the very end of the cumulative sum.
    for i in range(len(weights) - 1, -1, -1):
        if k >= count:
            break
        if i not in chosen and weights[i] > 0:
            chosen.append(i)
            k += 1
    return sorted(set(chosen))


def bucket_shuffle(items, time_of, rng, bucket_ms=BUCKET_MS):
    """Order items by time bucket, shuffled inside each bucket."""
    buckets = {}
    for item in items:
        buckets.setdefault(int(time_of(item) // bucket_ms), []).append(item)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        rng.shuffle(group)
        out.extend(group)
    return out


def allocate_exact(sessions, faults, rate_of, eligible, available, rng=random, time_of=lambda s: s["start_ms"]):
    """Assign faults to planned sessions with exact counts.

    sessions: planned session dicts. faults: fault dicts with "id". rate_of(fault, session) -> rate in [0, 1].
    eligible(fault, session, assigned) -> bool: the session counts toward the fault's rate (its denominator).
    available(fault, session, assigned) -> bool: the fault can be placed there given the session's other
    faults (exclusive groups, an earlier fatal crash, journey conflicts). assigned is the fault-id set so far.
    The count is fixed from every eligible session and placed only where available, so the emitted share
    of eligible sessions matches the request. Returns {session index: [fault ids]} and per-fault counts.
    """
    assigned = {i: [] for i in range(len(sessions))}
    report = {}
    order = bucket_shuffle(list(range(len(sessions))), lambda i: time_of(sessions[i]), rng)
    for fault in faults:
        idx = [i for i in order if eligible(fault, sessions[i], set(assigned[i]))]
        rates = {i: rate_of(fault, sessions[i]) for i in idx}
        expected = sum(rates.values())
        count = int(math.floor(expected + rng.random()))
        pool = [i for i in idx if rates[i] > 0 and available(fault, sessions[i], set(assigned[i]))]
        count = min(count, len(pool))
        for j in _systematic([rates[i] for i in pool], count, rng):
            assigned[pool[j]].append(fault["id"])
        report[fault["id"]] = {"eligible": len(idx), "available": len(pool), "expected": round(expected, 3),
                               "assigned": count}
    return assigned, report


class StreamingQuota:
    """Exact-at-block-boundary allocation for one fault in a live stream."""

    def __init__(self, rng=random, block=20):
        self.rng, self.block = rng, block
        self.slots = []
        self.cumulative = rng.random()
        self.granted = 0
        self.seen = 0

    def take(self, rate):
        if not self.slots:
            earned = int(math.floor(self.cumulative + rate * self.block)) - int(math.floor(self.cumulative))
            self.cumulative += rate * self.block
            positions = set(self.rng.sample(range(self.block), min(self.block, max(0, earned))))
            self.slots = [i in positions for i in range(self.block)]
        self.seen += 1
        hit = self.slots.pop(0)
        self.granted += hit
        return hit


def draw_probability(rate, rng=random):
    return rng.random() < rate
