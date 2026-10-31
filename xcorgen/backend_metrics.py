"""Cumulative service RED metrics derived from emitted SERVER spans.

One producer ID per run separates simultaneous runners. Counter resets have a
new start time and producer ID. Historical app-only sessions produce no points.
"""

import bisect
import copy
from .otlp import attrs

BOUNDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
MAX_AGE_MS = {"raw": 2 * 3600000, "aggregated": 120000}


def points(payload):
    for resource in payload.get("resourceMetrics", []):
        for scope in resource["scopeMetrics"]:
            for metric in scope["metrics"]:
                for kind in ("sum", "histogram"):
                    yield from metric.get(kind, {}).get("dataPoints", [])


def timely(payload, now_ms, policy="aggregated"):
    return all(
        -60000 <= now_ms - int(p["timeUnixNano"]) / 1e6 <= MAX_AGE_MS[policy]
        for p in points(payload)
    )


class REDMetrics:
    def __init__(self):
        self.services = {}

    def collect(self, deliveries, producer):
        touched = set()
        for d in deliveries:
            if d["target"] != "otlp" or d["kind"] != "traces":
                continue
            for rs in d["payload"]["resourceSpans"]:
                resource = {
                    a["key"]: next(iter(a["value"].values()))
                    for a in rs["resource"]["attributes"]
                }
                key = (resource["service.name"], resource.get("service.version", ""))
                for scope in rs["scopeSpans"]:
                    for span in scope["spans"]:
                        if span["kind"] != 2:
                            continue
                        start, end = int(span["startTimeUnixNano"]), int(
                            span["endTimeUnixNano"]
                        )
                        if key not in self.services:
                            # Aggregate across this run's pods, without falsely attributing totals to one pod.
                            r = {
                                k: v
                                for k, v in resource.items()
                                if not k.startswith(("k8s.pod.", "container.", "host."))
                            }
                            r["service.instance.id"] = producer
                            self.services[key] = dict(
                                resource={"attributes": attrs(r)},
                                start=start,
                                end=end,
                                count=0,
                                errors=0,
                                total=0.0,
                                buckets=[0] * (len(BOUNDS) + 1),
                            )
                        row = self.services[key]
                        row["end"] = max(row["end"], end)
                        duration = (end - start) / 1e9
                        row["count"] += 1
                        row["errors"] += span.get("status", {}).get("code") == 2
                        row["total"] += duration
                        row["buckets"][bisect.bisect_left(BOUNDS, duration)] += 1
                        touched.add(key)
        resources = []
        for key in sorted(touched):
            row = self.services[key]
            row["end"] = max(row["end"], row.get("exported", 0) + 1)
            row["exported"] = row["end"]
            common = dict(
                startTimeUnixNano=str(row["start"]), timeUnixNano=str(row["end"])
            )
            metrics = [
                {
                    "name": name,
                    "unit": "{request}",
                    "sum": {
                        "aggregationTemporality": 2,
                        "isMonotonic": True,
                        "dataPoints": [dict(common, asInt=str(row[field]))],
                    },
                }
                for name, field in [
                    ("http.server.request.count", "count"),
                    ("http.server.request.error.count", "errors"),
                ]
            ]
            metrics.append(
                {
                    "name": "http.server.request.duration",
                    "unit": "s",
                    "histogram": {
                        "aggregationTemporality": 2,
                        "dataPoints": [
                            dict(
                                common,
                                count=str(row["count"]),
                                sum=row["total"],
                                explicitBounds=list(BOUNDS),
                                bucketCounts=[str(n) for n in row["buckets"]],
                            )
                        ],
                    },
                }
            )
            resources.append(
                {
                    "resource": copy.deepcopy(row["resource"]),
                    "scopeMetrics": [
                        {
                            "scope": {
                                "name": "io.opentelemetry.http",
                                "version": "1.0.0",
                            },
                            "metrics": metrics,
                        }
                    ],
                }
            )
        return {"resourceMetrics": resources}
