# Native SDK traces and ANR samples (verified 2026-11-04)

Sources: `embrace-android-sdk` tag `9.3.1` (the version in the owner's dashboard; at HEAD, 9.4.0-SNAPSHOT, the emitters,
sampler and mapper used here differ only in error handling), `embrace-apple-sdk` `a2e0e5c3` (6.21.0), `embrace-web-sdk` 2.31.0 with the
2.29 and 2.31 captures in `reference/`, the Embrace backend in `~/Claudecode/go` and dashjs.

## Android thread blockage (ANR)

| Rule | Source |
| --- | --- |
| Heartbeat every 100 ms; blocked after 1000 ms without a response; interval starts at the last answered heartbeat | `BlockedThreadDetector.kt`, `ThreadBlockageBehaviorImpl.kt` (defaults) |
| One `Thread.getStackTrace()` sample per watchdog tick after the threshold; at most 80 samples carry a stack, later ones `sample_code` 1 with no thread data; max 1000 samples | `ThreadStacktraceSampler.kt`, `ThreadBlockageSampler.kt` |
| A sample whose stack equals the previous one omits `jvm.thread.state`, `thread_priority`, `frame_count`, `exception.stacktrace`; the backend reuses the previous stack | `ThreadBlockageSampler.getThreadBlockageSamples`, `go/lib/codec/session_anr.go` |
| Lines are `StackTraceElement.toString()` innermost first, joined by `\n`, cut to 200; `frame_count` is the full depth | `ThreadExt.kt`, `ThreadBlockageOtelMapper.kt` |
| Span `emb-thread-blockage`, `emb.type` `perf.thread_blockage`, `interval_code`; `last_known_time_unix_nano` only for an interval still running at payload time | `ThreadBlockageOtelMapper.kt` |
| Times are whole milliseconds; `sample_overhead` is milliseconds converted to ns | same |

The backend loads only three samples per interval into the flame graph: the first non-empty sample, the "best"
sample (middle sample among those containing the most common non-system package) and an ad-SDK sample
(`go/loader/session/anr/internal/anr_sample.go`). The dashboard defaults to **First Sample** (`sample_type`
filter, dashjs `useANRStaticFilters.ts`). Frames are colored by package prefix (`go/lib/common/category_jvm.go`):
for example `com.<app>` is Unknown, `java.util.concurrent` and `kotlinx.coroutines` Concurrency, `android.database.sqlite`,
`SharedPreferencesImpl.writeToFile`, `okio`, `java.io`, `libcore.io` IO, `com.squareup.moshi` Serialization,
`kotlin.reflect` Reflection, `android.view`/`android.widget`/`androidx.recyclerview`/`org.chromium` View,
`com.google.android.gms.ads` Ad SDKs, `com.google.firebase`/`com.google.zxing` SDK, `io.embrace` Embrace.

`xcorgen/anr_stacks.py` therefore generates different root causes per ANR and lets each blockage evolve over time.
The spec's three `blockage_cause` families stay valid and expand into stories:

- `worker_lock`: `ResultStore.awaitRefresh` timed wait, `SessionTokenStore` monitor contention from a tap (BLOCKED), Firebase Remote Config `ConfigCacheClient.getBlocking` (5 s wait per read), `runBlocking` on the main dispatcher.
- `migration`: Room migration on first database open, a Room query filling a cursor window, `SharedPreferences.commit()` from a tap (XML serialization then fsync), a cached feed parsed with Moshi's reflective adapter.
- `layout`: RecyclerView binding with per-row formatter construction, Compose lazy list decoding images, first WebView creation by a sponsored ad banner (retail), boarding pass barcode rendering pixel by pixel (travel).

Entry paths are real main-thread shapes: input dispatch from `MessageQueue.nativePollOnce` through Embrace's
Compose tap `EmbraceWindowCallback.dispatchTouchEvent` (9.3.1 `embrace-android-instrumentation-compose-tap`),
Choreographer frames, Compose recomposition and layout, coroutine dispatch and fragment transactions.

## Android auto-instrumented traces (9.3.1)

- **App startup** (`AppStartupTraceEmitter.kt`): `emb-app-startup-cold` from process start with `emb-process-init`
  [process start, `Application.onCreate` end] containing `emb-embrace-init`, then `emb-activity-init-delay`,
  `emb-activity-init` and `emb-activity-render` (API 29-30 and 33+; `emb-activity-first-draw` on 31-32). Warm traces
  start at the first activity and have only `emb-activity-init` and the render span. Root attributes
  `emb.startup_activity` (`Activity.getLocalClassName()`, so `ui.MainActivity`) and, on API 35+,
  `emb.startup_launch_reason` (the reason the process was created). `emb-embrace-init` carries
  `emb.app.version_startup_counter`, `<section>-duration-ms`, `init-cpu-pct`, `init-run-delay-pct`,
  `init-disk-read-kb`, `init-gc-count`, `thermal-*`, `seconds-since-*`, `mem-available-pct`, `prefs-file-bytes`,
  `art-compiler-filter` and `app-image-at-init` (`SdkInitActions.kt`, `SdkInitEnvironmentAttributes.kt`).
- **UI load** (`UiLoadTraceEmitter.kt`, `trackRender = false` in 9.3.1): `emb-<localClassName>-hot-time-to-initial-display`
  when the existing activity restarts, `-cold-` when it is recreated, `emb.type` `perf.ui_load`, children
  `emb-<activity>-create` (cold), `-start`, `-resume`; it ends at `onActivityPostResumed`. UI loads that are part of app
  startup are not traced, so the generator emits them on returns from the background.
- Not emitted: the private `emb-sdk-init` span (never listed in Traces; its `ended-in-foreground` value depends on the
  process state during init) and vitals spans (`smoothness`, `screen-load`), which are remote-config gated.
- Network spans stay root spans: `NetworkRequestDataSourceImpl` parents them only when the app sets a traceparent.

## Apple (6.21.0)

- **Startup** (`StartupInstrumentation.swift`, `SpanSemantics+Startup.swift`): `emb-app-startup-cold|warm`
  (`sys.startup`), children `emb-app-pre-main-init` (not prewarmed), `emb-app-first-frame-rendered`,
  `emb-app-startup-app-init`, `emb-sdk-setup`, `emb-sdk-start`; every span has `isPrewarmed`. A launch is cold only
  without a stored build UUID and boot time, or when both changed (`DefaultStartupDataProvider.swift`).
- **View controllers** (`UIViewControllerHandler.swift`, first render on by default): `emb-<Class>-time-to-first-render`
  (`perf.ui_load`, `view.name` class, `view.title`) with `emb-view-did-load`, `-will-appear`, `-is-appearing`,
  `-did-appear`; `emb-screen-view` starts when viewDidAppear ends.
- **Hangs** (`HangCaptureService.swift`, `HangLimits.swift`): threshold 249 ms; one `emb-thread-blockage` span with
  `last_known_time_unix_nano` (start), `interval_code` "0", `thread_priority` "0" and at most one
  `thread_blockage_sample` event at the hang start: `sample_overhead` (ns), `frame_count`, `emb.stacktrace.ios`
  (base64 JSON frames `a, m, o, p, s, so, u`, demangled on device, innermost first). `xcorgen/hang_stacks.py`.

## Web (2.31.0)

No startup or UI load trace exists; page load is `documentLoad` (mapped to page loads, not listed in Traces).
XHR spans are named by the HTTP method and end with `loaded` (status < 299) or `error`
(`@opentelemetry/instrumentation-xml-http-request` `xhr.js`, 2.29 capture). Business spans get the same child
spans as native apps through the app's tracer.

## Business spans

`xcorgen/business_spans.py` adds the children an app team instruments under each business root span: local work
before the request (cache or database reads, validation, tokenization), one child wrapping the request, and
decode, persist and render work after a successful response, scaled by device speed. A slow root span fault lands in
the heaviest local step.

`tools/check_output.py` validates all of the above (`check_native_traces`); `tests/test_native_traces.py` covers it.
