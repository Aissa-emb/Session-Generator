"""Compatibility import and OTLP timestamp shifting; BrowserSession is the web adapter."""
from .browser_session import BrowserSession as WebSession
from . import otlp

TIME_KEYS = ("startTimeUnixNano", "endTimeUnixNano", "timeUnixNano", "observedTimeUnixNano")


def shift(obj, delta_ns):
    """Shift every OTLP timestamp in a payload (used to place a simulated session in the past)."""
    if isinstance(obj, dict):
        resources = obj.get("resourceSpans", []) + obj.get("resourceLogs", [])
        if resources:
            r = {a["key"]: a["value"] for a in resources[0].get("resource", {}).get("attributes", [])}
            if r.get("sdk_platform", {}).get("stringValue") == "web" and "Chrome/" not in r.get("user_agent.original", {}).get("stringValue", ""):
                delta_ns = round(delta_ns / 1000000) * 1000000
        for k, v in obj.items():
            if k in TIME_KEYS and isinstance(v, str):
                obj[k] = str(int(v) + delta_ns)
            elif k == "key" and v in ("emb.user_session_start_ts", "dom_state.images_above_fold.timestamp"):
                value = next(iter(obj["value"].values())) + delta_ns / 1e6
                obj["value"] = otlp.value(int(value) if value == int(value) else round(value, 1))
            else:
                shift(v, delta_ns)
    elif isinstance(obj, list):
        for x in obj:
            shift(x, delta_ns)
