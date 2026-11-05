"""Android crash and main-thread blockage adapters grounded in SDK schemas."""
import json

from .milestones import mark

FAMILIES = (
    ("java.lang.NullPointerException", "Required response field was null", "mapResult", 84),
    ("java.lang.IllegalStateException", "Cannot access view after onDestroyView", "onResult", 126),
    ("java.lang.IndexOutOfBoundsException", "Index 3 out of bounds for length 3", "onItemSelected", 67),
    ("java.lang.NumberFormatException", 'For input string: "1.249,00"', "parseAmount", 52),
    ("android.database.sqlite.SQLiteException", "no such column: updated_at (code 1 SQLITE_ERROR)", "migrate", 41),
)
CLASSES = {
    "ecommerce": ("CheckoutResultMapper", "CheckoutFragment", "CartAdapter", "PriceParser", "CatalogCacheMigration"),
    "banking": ("AccountSummaryMapper", "TransferFragment", "TransactionAdapter", "AmountParser", "AccountCacheMigration"),
    "travel": ("BookingConfirmationMapper", "BookingFragment", "ItineraryAdapter", "FareParser", "ItineraryCacheMigration"),
}


def fault(s, fatal=False):
    if getattr(s,"platform",None)=="ios":
        s.exception("NSInvalidArgumentException","Required response field was unavailable",handled=not fatal)
        return
    family = s.plan.crash_family if fatal else 3
    exception, message, method, line = FAMILIES[family]
    cls = CLASSES[s.profile.key][family]
    custom = (getattr(s.plan, "crash_details", None) or {}) if fatal else {}
    if custom.get("exception"):
        # A spec-defined crash keeps the family's stage and milestone, with its own type and frame.
        exception, message = custom["exception"], custom.get("message", message)
        method, cls, line = custom.get("method", method), custom.get("class", cls), custom.get("line", line)
    package = s.app["package"] if hasattr(s, "app") else s.profile.android["package"]
    frames = [f"{package}.{s.profile.key}.{cls}.{method}({cls}.kt:{line})",
              "kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith(ContinuationImpl.kt:33)",
              "android.os.Handler.handleCallback(Handler.java:995)", "android.os.Looper.loop(Looper.java:338)"]
    mark(s, ("response_validation_failed", "callback_arrived_after_navigation", "selection_changed_during_refresh",
             "localized_value_received", "cache_upgrade_started")[family])
    if not hasattr(s, "_crash"):
        s.exception(exception.rsplit(".", 1)[-1], message, handled=not fatal, function=method)
        if fatal:
            s.leave()
            from .android_session import SessionEnded
            raise SessionEnded()
        return
    if fatal:
        # Model the next launch as the upload opportunity for the persisted
        # crash. No UI executes in the dead process after _crash raises.
        s.persona["pending_crash_upload"] = True
        s.crash_upload_due = s.t + s.rng.uniform(8, 35) * 1000
        s._crash(exception, message, frames)
    else:
        from .android_session import kv, nanos
        s.exception_number += 1
        a = dict(s._log_common(), **{"emb.type": "sys.exception", "emb.exception_handling": "handled",
            "emb.private.send_mode": "IMMEDIATE", "exception.type": exception.rsplit(".", 1)[-1],
            "exception.message": message, "exception.stacktrace": json.dumps(frames)})
        s._use("log_exception")
        s.part.logs.append((s.t, {"time_unix_nano": nanos(s.t), "severity_number": 17,
                                 "severity_text": "ERROR", "body": message, "attributes": kv(a)}))


def followup(s):
    """The UI layer catches a failed response and reports it with Embrace.logException (handled)."""
    failed = next(((key, status) for key, status, _ in reversed(s.calls) if status is None or status >= 400), None)
    if failed is None or not hasattr(s, "_crash"):
        return
    if getattr(s,"platform",None)=="ios":
        s.exception("NSError","Request failed",handled=True)
        return
    from .android_session import kv, nanos
    from .catalog import NETWORK_FAILURES
    key, status = failed
    if status is None:
        exc, message, _ = NETWORK_FAILURES.get(getattr(s.plan, "transport", None) or "timeout")
        message = message.format(host=s.api_host)
        frames = ["okhttp3.internal.connection.RealCall.getResponseWithInterceptorChain$okhttp(RealCall.kt:201)",
                  "okhttp3.internal.connection.RealCall$AsyncCall.run(RealCall.kt:517)"]
    else:
        exc, message = "retrofit2.HttpException", f"HTTP {status} "
        frames = ["retrofit2.KotlinExtensions$await$2$2.onResponse(KotlinExtensions.kt:53)",
                  "retrofit2.OkHttpCall$1.onResponse(OkHttpCall.java:164)",
                  "okhttp3.internal.connection.RealCall$AsyncCall.run(RealCall.kt:529)"]
    frames += ["java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1145)",
               "java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:644)",
               "java.lang.Thread.run(Thread.java:1012)"]
    s.exception_number += 1
    a = dict(s._log_common(), **{"emb.type": "sys.exception", "emb.exception_handling": "handled",
        "emb.private.send_mode": "IMMEDIATE", "exception.type": exc.rsplit(".", 1)[-1],
        "exception.message": message, "exception.stacktrace": json.dumps(frames)})
    s._use("log_exception")
    s.part.logs.append((s.t, {"time_unix_nano": nanos(s.t), "severity_number": 17,
                             "severity_text": "ERROR", "body": message, "attributes": kv(a)}))


def blockage(s):
    mark(s, "main_thread_work_started")
    spec = getattr(s.plan, "blockage_details", None) or {}
    family = spec.get("cause") or s.plan.settings.get("blockage_cause") or s.rng.choice(("migration", "worker_lock", "layout"))
    duration = s.rng.uniform(spec.get("min_ms", 6000), spec.get("max_ms", 12000))
    start = s.t
    story = cause = family
    if hasattr(s, "native_blockage"):
        story = s.native_blockage(duration, family) or family
    elif hasattr(s, "_crash"):
        from .android_session import kv, nanos
        from . import anr_stacks
        story = anr_stacks.choose_story(family, s.profile.key, s.rng)
        cause = family if family in anr_stacks.FAMILIES else anr_stacks.family_of(story)
        screen = anr_stacks.Screen(s.profile.key, (s.screen or {}).get("name"), s.persona["device_id"])
        frames = anr_stacks.Frames(s.persona.get("os_code", "35"), s.persona.get("manufacturer"), s.app["package"])
        # The SDK clock reports whole milliseconds; the interval ends when the queued heartbeat is handled.
        begin, end = int(start), int(start + duration) + s.rng.randint(1, 12)
        samples = anr_stacks.blockage_samples(s.rng, story, screen, frames, begin, end)
        events = [{"name": "perf.thread_blockage_sample", "time_unix_nano": nanos(t), "attributes": kv(a)}
                  for t, a in samples]
        s._span("emb-thread-blockage", begin, end, {"emb.type": "perf.thread_blockage", "interval_code": 0},
                events=events)
        duration = end - start
    elif hasattr(s, "long_task"):
        s.long_task(duration)
    else:
        # A browser main-thread task is not an Android ANR.
        with s.flow("render_result_list"):
            s.t += duration
        s.t = start
    s.t = start + duration
    s.blockage_evidence = {"cause": cause, "story": story, "start": start, "end": s.t, "os_confirmed": False}
    mark(s, "main_thread_responsive")
