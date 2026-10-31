"""OTLP helpers: JSON-shaped builders plus a small protobuf encoder.

Spans and logs are built as OTLP/JSON dicts (the shape the Embrace Web SDK
sends). The same dicts are encoded to OTLP protobuf for backends that only
accept application/x-protobuf, such as the Chronosphere OTLP/HTTP endpoint.
"""

import struct

SPAN_KIND = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2


def value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [value(x) for x in v]}}
    if v is None:
        return {}
    return {"stringValue": str(v)}


def attrs(d):
    """dict -> OTLP KeyValue list, skipping None values."""
    return [{"key": k, "value": value(v)} for k, v in d.items() if v is not None]


# --- protobuf encoding -------------------------------------------------------

def _varint(n):
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field, wire):
    return _varint((field << 3) | wire)


def _len(field, payload):
    return _key(field, 2) + _varint(len(payload)) + payload


def _str(field, s):
    return _len(field, s.encode()) if s else b""


def _bytes_hex(field, h):
    return _len(field, bytes.fromhex(h)) if h else b""


def _uint(field, n):
    return _key(field, 0) + _varint(int(n)) if n else b""


def _fixed64(field, n):
    return _key(field, 1) + struct.pack("<Q", int(n)) if n else b""


def _fixed32(field, n):
    return _key(field, 5) + struct.pack("<I", int(n)) if n else b""


def _any(v):
    if "stringValue" in v:
        return _len(1, v["stringValue"].encode())
    if "boolValue" in v:
        return _key(2, 0) + _varint(1 if v["boolValue"] else 0)
    if "intValue" in v:
        return _key(3, 0) + _varint(int(v["intValue"]))
    if "doubleValue" in v:
        return _key(4, 1) + struct.pack("<d", float(v["doubleValue"]))
    if "arrayValue" in v:
        return _len(5, b"".join(_len(1, _any(x)) for x in v["arrayValue"].get("values", [])))
    return b""


def _kvs(field, kvs):
    return b"".join(_len(field, _str(1, kv["key"]) + _len(2, _any(kv["value"]))) for kv in kvs or [])


def _resource(r):
    return _kvs(1, r.get("attributes"))


def _scope(s):
    return _str(1, s.get("name", "")) + _str(2, s.get("version", "")) + _kvs(3, s.get("attributes"))


def _span(s):
    out = _bytes_hex(1, s["traceId"]) + _bytes_hex(2, s["spanId"]) + _str(3, s.get("traceState", ""))
    out += _bytes_hex(4, s.get("parentSpanId", "")) + _str(5, s["name"]) + _uint(6, s.get("kind", 0))
    out += _fixed64(7, s["startTimeUnixNano"]) + _fixed64(8, s["endTimeUnixNano"]) + _kvs(9, s.get("attributes"))
    for e in s.get("events", []):
        out += _len(11, _fixed64(1, e["timeUnixNano"]) + _str(2, e["name"]) + _kvs(3, e.get("attributes")))
    for link in s.get("links", []):
        out += _len(13, _bytes_hex(1, link["traceId"]) + _bytes_hex(2, link["spanId"]) + _kvs(4, link.get("attributes")))
    if "status" in s:
        st = s["status"] or {}
        out += _len(15, _str(2, st.get("message", "")) + _uint(3, st.get("code", 0)))
    return out + _fixed32(16, s.get("flags", 0))


def _log(r):
    out = _fixed64(1, r.get("timeUnixNano", 0)) + _uint(2, r.get("severityNumber", 0)) + _str(3, r.get("severityText", ""))
    if "body" in r:
        out += _len(5, _any(r["body"] or {}))
    out += _kvs(6, r.get("attributes")) + _fixed32(8, r.get("flags", 0))
    out += _bytes_hex(9, r.get("traceId", "")) + _bytes_hex(10, r.get("spanId", ""))
    return out + _fixed64(11, r.get("observedTimeUnixNano", 0)) + _str(12, r.get("eventName", ""))


def encode_traces(req):
    """ExportTraceServiceRequest (OTLP/JSON dict) -> protobuf bytes."""
    out = b""
    for rs in req["resourceSpans"]:
        body = _len(1, _resource(rs["resource"]))
        for ss in rs["scopeSpans"]:
            body += _len(2, _len(1, _scope(ss["scope"])) + b"".join(_len(2, _span(s)) for s in ss["spans"]))
        out += _len(1, body)
    return out


def encode_logs(req):
    """ExportLogsServiceRequest (OTLP/JSON dict) -> protobuf bytes."""
    out = b""
    for rl in req["resourceLogs"]:
        body = _len(1, _resource(rl["resource"]))
        for sl in rl["scopeLogs"]:
            body += _len(2, _len(1, _scope(sl["scope"])) + b"".join(_len(2, _log(r)) for r in sl["logRecords"]))
        out += _len(1, body)
    return out


def _double(field, value):
    return _key(field, 1) + struct.pack("<d", float(value))


def encode_metrics(req):
    """ExportMetricsServiceRequest: cumulative integer sums and explicit histograms."""
    out = b""
    for rm in req["resourceMetrics"]:
        resource = _len(1, _resource(rm["resource"]))
        for sm in rm["scopeMetrics"]:
            scope = _len(1, _scope(sm["scope"]))
            for metric in sm["metrics"]:
                body = _str(1, metric["name"]) + _str(2, metric.get("description", "")) + _str(3, metric.get("unit", ""))
                kind = "sum" if "sum" in metric else "histogram"
                data = metric[kind]
                encoded = b""
                for point in data["dataPoints"]:
                    p = _fixed64(2, point["startTimeUnixNano"]) + _fixed64(3, point["timeUnixNano"])
                    if kind == "sum":
                        p += _key(6, 1) + struct.pack("<q", int(point["asInt"])) + _kvs(7, point.get("attributes"))
                    else:
                        p += _fixed64(4, point["count"]) + _double(5, point["sum"]) + _kvs(9, point.get("attributes"))
                        p += _len(6, b"".join(struct.pack("<Q", int(n)) for n in point["bucketCounts"]))
                        p += _len(7, b"".join(struct.pack("<d", float(n)) for n in point["explicitBounds"]))
                    encoded += _len(1, p)
                encoded += _uint(2, data["aggregationTemporality"])
                if kind == "sum":
                    encoded += _uint(3, data.get("isMonotonic", False))
                scope += _len(2, body + _len(7 if kind == "sum" else 9, encoded))
            resource += _len(2, scope)
        out += _len(1, resource)
    return out


def _read_varint(b, i):
    n = 0
    for shift in range(0, 70, 7):
        if i >= len(b):
            raise ValueError("Truncated OTLP varint")
        c = b[i]
        i += 1
        if shift == 63 and c > 1:
            raise ValueError("Oversized OTLP varint")
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
    raise ValueError("Oversized OTLP varint")


def _response_fields(body):
    i = 0
    while i < len(body):
        key, i = _read_varint(body, i)
        field, wire = key >> 3, key & 7
        if not field or field >= 1 << 29:
            raise ValueError("Invalid OTLP field")
        if wire == 0:
            value, i = _read_varint(body, i)
        else:
            if wire == 2:
                size, i = _read_varint(body, i)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError("Invalid OTLP wire type")
            if size > len(body) - i:
                raise ValueError("Truncated OTLP field")
            value = body[i:i + size]
            i += size
        yield field, wire, value


def partial_success(body, json_body=False):
    """(rejected count, error message) from an OTLP export response; (0, "") when everything was accepted."""
    if not body:
        return 0, ""
    if json_body:
        import json
        ps = json.loads(body).get("partialSuccess") or {}
        return int(ps.get("rejectedSpans") or ps.get("rejectedLogRecords") or ps.get("rejectedDataPoints") or 0), ps.get("errorMessage", "")
    rejected, message = 0, ""
    for field, wire, value in _response_fields(body):
        if field == 1:
            if wire != 2:
                raise ValueError("Invalid OTLP partial success")
            for field, wire, value in _response_fields(value):
                if field == 1:
                    if wire != 0 or value >= 1 << 63:
                        raise ValueError("Invalid OTLP rejected count")
                    rejected = value
                elif field == 2:
                    if wire != 2:
                        raise ValueError("Invalid OTLP error message")
                    message = value.decode(errors="replace")
    return rejected, message
