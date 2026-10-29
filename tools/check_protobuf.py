#!/usr/bin/env python3
"""Check xcorgen's hand-written OTLP protobuf encoder against the official OpenTelemetry protobuf definitions.

Needs the opentelemetry-proto package (keep it out of the generator itself, which is stdlib only):
  python3 -m venv /tmp/pbvenv && /tmp/pbvenv/bin/pip install opentelemetry-proto
  /tmp/pbvenv/bin/python tools/check_protobuf.py out/
Every OTLP payload in a --dry-run directory is encoded, parsed back, and compared field by field.
"""
import glob, json, sys, base64
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from xcorgen import otlp
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

def norm_ids(obj):
    # OTLP/JSON uses hex IDs; protobuf's canonical JSON uses base64 for bytes.
    if isinstance(obj, dict):
        for k in ('traceId', 'spanId', 'parentSpanId'):
            if k in obj and obj[k]:
                obj[k] = base64.b64encode(bytes.fromhex(obj[k])).decode()
        for v in obj.values(): norm_ids(v)
    elif isinstance(obj, list):
        for v in obj: norm_ids(v)
    return obj

n = bad = 0
files = sorted(glob.glob(sys.argv[1] + '/*otlp_*.json')) + sorted(glob.glob(sys.argv[1] + '/*embrace_*.json'))
for f in files:
    d = json.load(open(f))
    p = d['payload']
    is_trace = 'resourceSpans' in p
    raw = (otlp.encode_traces if is_trace else otlp.encode_logs)(p)
    msg = (ExportTraceServiceRequest if is_trace else ExportLogsServiceRequest)()
    msg.ParseFromString(raw)
    # Reference: parse our JSON with the official parser, then compare messages field by field.
    ref = (ExportTraceServiceRequest if is_trace else ExportLogsServiceRequest)()
    json_format.ParseDict(norm_ids(json.loads(json.dumps(p))), ref, ignore_unknown_fields=False)
    n += 1
    if msg != ref:
        bad += 1
        if bad < 3:
            print('MISMATCH', f)
print(f'{n} payloads round-tripped, {bad} mismatches')
