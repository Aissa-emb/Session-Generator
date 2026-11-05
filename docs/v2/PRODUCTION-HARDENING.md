# Throughput and resource audit — 2026-11-05

The live control-plane worker now sustains the UI's 600 sessions/minute setting on the owner's Mac against a local TLS receiver with 150 ms latency per request. It uses an anchored arrival schedule, bounded catch-up, rate-scaled senders, persistent TLS connections, and batched observational writes. Every generated session still passes realism lint before sending. Strict bounded-run intent/receipt commits and replay checks remain synchronous.

This is capacity evidence for a local installation and the measured scenario. It does not establish real destination quotas, dashboard retention, or capacity for a shared 20,000-user server. Real sends, source-map uploads, merge and installation require the owner's separate approval.

## Measurements

The fixture is `tools/fixtures/load-web.json`, the web scenario from the reported failure, with destinations overridden to loopback by the harness. Requested rate: 600/min; receiver: certificate-verified TLS, HTTP/1.1, 150 ms delay per POST. No destination credentials or remote upload APIs are used.

| Measurement | Unchanged main `4a544e8` | Hardened worker |
| --- | --- | --- |
| Generation window | 1 minute | 10 minutes |
| Generated sessions | 200 | 5,881 |
| Achieved average | 200/min | 588.1/min |
| Completed payloads | 4,027 | 117,758 |
| Delivery errors / rejected records | 0 / 0 | 0 / 0 |
| Missed arrivals | Not reported | 0 |
| Peak RSS, worker plus in-process receiver | 412.6 MiB | 1,016.6 MiB |
| SQLite including WAL after drain | 7.98 MiB | 12.39 MiB |
| Total wall time including scheduled export drain | 182 seconds | 744 seconds |

Poisson arrivals vary around the request: 5,881 is 1.54 standard deviations below 6,000. A separate earlier 10-minute run achieved 595/min with zero errors. The shorter baseline is a throughput comparison, not a long-duration memory comparison. Its final results were recorded successfully; teardown then encountered the old Store API's lack of `close()`.

The final run's RSS reached its high-water mark after roughly four minutes; the database remained about 12–13 MiB. RSS includes Python objects, thread stacks, the receiver, and allocator high-water retention. The 256 MiB queue guard measures serialized payload bytes, not process RSS. Peak memory therefore must not be described as 256 MiB.

Under concurrent load, first-minute generation/lint/record averaged 6.45/12.95/0.53 ms; the last full minute averaged 9.45/12.17/0.65 ms. A small final boundary bucket included a slow in-progress generation call and is retained in the raw result, rather than presented as a full minute. To distinguish pool warmup and machine contention from growing history, a separate 10,000-session offline test saved the pool and summarized every simulated five seconds:

| Sessions processed | Generate | Lint | Record | Save + summary amortized | Total ms/session |
| --- | --- | --- | --- | --- | --- |
| 1,000 | 4.84 | 9.78 | 0.60 | 0.27 | 15.49 |
| 4,000 | 5.79 | 9.62 | 0.64 | 1.45 | 17.50 |
| 5,000 | 6.07 | 9.59 | 0.71 | 1.68 | 18.04 |
| 10,000 | 5.85 | 9.68 | 0.77 | 1.67 | 17.97 |

Cost stays flat after the existing 4,000-visitor pool fills. The pool is deliberately bounded; its initial warmup is not eliminated.

Other measured results:

- Linter equivalence: 1,440 comparisons covering Android/web/iOS/live web, plus 360 incident comparisons; `max_issues` 50/3/1 and approximately 3% injected forbidden/PII values. Identical issue order and exceptions. Mixed-platform lint fell from 23.35 to 4.90 ms/session (4.77×); incident comparison improved 3.77×. The temporary reference implementation was removed.
- At 100,000 receipts, identical delivery summary results took 299.67 ms by history scan versus 0.0025 ms from stored aggregates. Reading the indexed latest 500 receipts took 1.57 ms. These timings exclude HTTP overhead.
- Planning 50,000 backfill sessions took 14.41 seconds with 164.55 MiB peak RSS. This measures quota metadata planning, not a completed 50,000-session send. Generated payloads now stage one session at a time on disk.

## Limits ranked by impact

| Impact / path | Finding and change | Bound or retained behavior |
| --- | --- | --- |
| Critical: live scheduling | One session per pass, fixed 200 ms sleep, and re-anchoring lost arrivals. Use `next_start += interval`, up to 32 sessions or 0.5 seconds per pass, and event-driven 10–200 ms sleeps. Read controls/spec before each session. | More than 30 seconds of lag advances the schedule and reports estimated missed arrivals. Elapsed time uses a monotonic clock anchored to wall time. Finite duration stays fixed across pause. |
| Critical: sender throughput | Eight workers plus new TLS for every POST constrained output. Live workers scale as `ceil(rate/12)`, clamped 8–64; submissions never block the control loop. | Four pending slots per worker; four origins per thread; due exports serviced first. Persistent connections keep certificate checks, response limits, redirect refusal and sanitized errors. Proxy traffic retains urllib behavior. |
| High: per-payload writes | Two FULL commits and a console progress log per receipt. Live receipts now flush up to 512 rows per transaction every 200 ms. | Queue 2,048 receipts. Counters/events publish about once/sec; pool/report every five seconds and transitions/exit. Writer errors fail the run. FULL synchronous mode remains. |
| High: growing live memory | Full observation history, queued payloads, and retained senders grew with a run. | Cumulative session/fault counts plus latest 128 detail observations; pending sessions 2,048; serialized queue 256 MiB; at most four draining spec-version senders and 32 old version reports. Old senders close threads/connections after draining. |
| High: live disk growth | Receipt/event tables, manifests, dry payloads and launcher logs grew indefinitely. | Every five seconds retain latest 10,000 live receipts, 2,000 events and 1,000 live dry payload files. Manifest rotates at 16 MiB with one archive. Launcher log rotates at 8 MiB with three archives. Allow one interval/record of overshoot. Counts remain cumulative. |
| High: bounded batch payloads | `run_batch` retained every payload. UI advertised 100,000 despite an engine limit of 50,000. | Private compressed SQLite spool, 4 MiB cache, 4 GiB compressed payload budget, at least 512 MiB free space before each 1,000 additions. UI/schema now agree with 50,000. Complete realism preflight precedes the first POST; backend-first order and strict resume retained. Exact allocation metadata remains O(session count). |
| High: status polling | Every two-second poll rescanned all receipts. Bound queries did not use JSON-path indexes. | Atomic cumulative acknowledgements; duplicate-safe bounded rollup; run/created and event/cursor indexes, explicitly installed on existing databases. Legacy finished runs retain a history fallback. Token lookup indexed; last-used writes at most once/minute per serial caller. |
| High: intermittent errors | Twenty failures across an entire run stopped it. | Stop at 10 consecutive failures or at least 20 of the last 200. Lifetime error counts remain visible. |
| Medium: visitor/runtime caches | Pool save was per-session; oversized loaded files could exceed the pool limit. | Save every five seconds; trim oversized loads to existing size (600 default, 4,000 for shared populations). Runtime caches now hard-cap both lookup-only misses and unexpired fills at 20,000 entries; expiry alone was insufficient. Existing operation cap 20,000, per-visit modeling rows 512, and causal report evidence 120 remain. |
| Medium: request/process resources | Unlimited simultaneous previews/jobs and slow body reads could exhaust one local host. | Two previews, 120-second subprocess timeout, four queued/running jobs per host, existing four live streams per target. Pause/stop bypass launch lock. Body limit remains 1 MiB with a 15-second read timeout. SQLite engines and rejected-resume journals now close explicitly. |
| Medium: source maps | Multiple sequential uploads had no overall budget. | Stop starting uploads after 90 seconds; each request still has a 30-second timeout. Persist accepted bundles so retry continues. The final in-flight request can extend elapsed time beyond 90 seconds. |
| Medium: user feedback | Requested rate alone concealed slower generation. | Show achieved rolling-minute rate next to requested rate and a capacity notice for CPU, sender saturation or errors. Capacity notice persists for 60 seconds after pressure; drain preserves the last generation rate. |

Reports explicitly label detail percentiles, modeled evidence and population breakdowns as recent observations when using the bounded detail window (live runs and batches above 2,000). Session and fault counts stay cumulative for the current process segment/version. A resume starts a new live report segment; it does not reconstruct lost detail history. API acknowledgements remain separate from dashboard ingestion.

## Durability and deliberate limits

Bounded runs still commit their RunJournal intent before every POST and receipt after it. Ambiguous and partial deliveries block automatic replay; accepted deliveries are never resent. The new acknowledgement tables are display aggregates, not replay authority. Disk staging is disposable, mode 600, contains JSON rather than executable pickle, and is removed on orderly exit. A process kill can leave a private spool directory in the OS temp directory; it is never reused for resume.

Live jobs never resend unfinished exports after a crash. Receipt batching can lose the final 200 ms plus any queued persistence backlog of observational receipts on abrupt termination; it cannot cause an automatic duplicate POST. Orderly exit flushes them. The manifest records completed sessions independently. Rotated history is marked retained/recent; verification cannot claim complete historical coverage after truncation.

The following are deliberately preserved:

- FULL SQLite durability and strict server receipt transport (no retry). Legacy non-strict CLI transport still permits up to three attempts for transient/429/5xx failures. Persistent connections do not automatically replay a failed POST.
- Historical run records, saved spec versions and bounded-run journals. These are recovery/audit data and are not automatically deleted. Total disk across arbitrarily many runs is therefore not globally bounded; the limits above bound an active live run. Archive completed runs through an operator-managed backup/retention policy. No automatic `VACUUM` stalls active workers; SQLite reuses freed pages. Existing databases can retain a larger historical high-water file size.
- SDK envelope formats, trace freshness/forwarding rules, quota semantics, and the old V1 LaunchAgent. Legacy CLI manifests and historical V1 output directories are outside the server's rolling retention policy.
- The 600/min UI ceiling. The measured host sustains it for this web workload, so no lower arbitrary warning was added. Complex scenarios, multiple concurrent jobs, remote latency, proxy behavior and destination quotas can lower capacity; the running view reports the observed result.

## Reproduce and validate

From the repo root with server dependencies installed:

```sh
.venv/bin/python tools/load_live.py --rate 600 --minutes 10 --latency-ms 150
python3 tools/benchmark_live.py --sessions 10000
python3 -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s tests/server -v
.venv/bin/python tools/generate_models.py --check
npm --prefix apps/web run schema:check
npm --prefix apps/web run build
SG_UI_PORT=8013 npm --prefix apps/web test
```

The load harness creates a temporary trusted certificate and loopback receiver, overrides both telemetry destinations, and never invokes source-map upload. It is intentionally outside CI. Regression coverage includes catch-up, pause/stop, error windows, throttling, capacity reporting, linter edge cases, bounded reports/pools, TLS connection reuse and safeguards, spool ordering/preflight, duplicate-safe counts, index upgrades, retention, preview limits and request timeouts.

GitHub Actions is enabled. The previously observed failed workflow stopped on a requested-versus-emitted latency tolerance check; it was not disabled. Review the checks on this PR before merging. Install only after owner approval and confirming no active runs, using `tools/studio.py stop/start`; verify `/health` and a browser preview afterward.
