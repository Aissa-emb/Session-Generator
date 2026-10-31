"""Serialize control-plane check-and-create transactions across threads/processes."""

import fcntl
from functools import wraps


def serialized(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(svc, *args, **kwargs):
            with (svc.store.directory / (name + ".lock")).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return fn(svc, *args, **kwargs)

        return wrapped

    return decorate
