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
    family = s.plan.crash_family if fatal else 3
    exception, message, method, line = FAMILIES[family]
    cls = CLASSES[s.profile.key][family]
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


def blockage(s):
    mark(s, "main_thread_work_started")
    cause = s.plan.settings.get("blockage_cause") or s.rng.choice(("migration", "worker_lock", "layout"))
    duration = s.rng.uniform(6000, 12000)
    start = s.t
    package = s.app["package"] if hasattr(s, "app") else s.profile.android["package"]
    frames = {
        "migration": ["android.database.sqlite.SQLiteConnection.nativeExecute(Native Method)",
                      f"{package}.{s.profile.key}.LocalCache.migrate(LocalCache.kt:71)"],
        "worker_lock": ["java.lang.Object.wait(Native Method)",
                        f"{package}.{s.profile.key}.ResultStore.awaitRefresh(ResultStore.kt:94)"],
        "layout": [f"{package}.{s.profile.key}.ResultAdapter.bindItems(ResultAdapter.kt:138)",
                   "android.view.ViewRootImpl.performTraversals(ViewRootImpl.java:4019)"]}[cause]
    if hasattr(s, "_crash"):
        from .android_session import kv, nanos
        events = [{"name": "perf.thread_blockage_sample", "time_unix_nano": nanos(start + offset),
                   "attributes": kv({"emb.type": "perf.thread_blockage_sample", "sample_code": 0,
                       "sample_overhead": 1_000_000, "jvm.thread.state": "WAITING" if cause == "worker_lock" else "RUNNABLE",
                       "thread_priority": 5, "frame_count": len(frames), "exception.stacktrace": "\n".join(frames)})}
                  for offset in range(500, int(duration), 500)]
        s._span("emb-thread-blockage", start, start + duration,
                {"emb.type": "perf.thread_blockage", "interval_code": 0,
                 "last_known_time_unix_nano": nanos(start + duration)}, events=events)
    else:
        # A browser main-thread task is not an Android ANR.
        with s.flow("render_result_list"):
            s.t += duration
        s.t = start
    s.t = start + duration
    s.blockage_evidence = {"cause": cause, "start": start, "end": s.t, "os_confirmed": False}
    mark(s, "main_thread_responsive")
