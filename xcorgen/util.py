import math
import random
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_identity_rng = ContextVar("emission_identity_rng", default=None)


@contextmanager
def identity_scope(rng):
    """Separate per-emission IDs from reproducible simulation draws."""
    token = _identity_rng.set(rng)
    try:
        yield
    finally:
        _identity_rng.reset(token)


def hex_id(n_bytes, rng=random, upper=False):
    identity_rng = getattr(rng, "identity_rng", None) or _identity_rng.get() or rng
    h = "%0*x" % (n_bytes * 2, identity_rng.getrandbits(n_bytes * 8))
    return h.upper() if upper else h


def emb_id(rng=random):
    """32 uppercase hex chars, the format the Web SDK uses for session, part, page and log IDs."""
    return hex_id(16, rng, upper=True)


def uuid4(rng=random):
    identity_rng = getattr(rng, "identity_rng", None) or _identity_rng.get() or rng
    return str(uuid.UUID(int=identity_rng.getrandbits(128), version=4))


def lat(p50, p95, rng=random):
    """Lognormal latency in ms with the given median and 95th percentile."""
    sigma = math.log(p95 / p50) / 1.645 if p95 > p50 else 0.0
    return p50 * math.exp(rng.gauss(0, 1) * sigma)


def weighted(items, rng=random):
    """items: list of (value, weight) -> value."""
    total = sum(w for _, w in items)
    r = rng.uniform(0, total)
    for v, w in items:
        r -= w
        if r <= 0:
            return v
    return items[-1][0]


def ns(ms_epoch):
    return int(round(ms_epoch * 1_000_000))
